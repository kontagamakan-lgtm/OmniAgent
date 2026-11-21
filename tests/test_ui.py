"""Tests du livrable 5 sans affichage : contrôleur, point d'entrée, garde-fous.

La fenêtre elle-même n'est pas instanciée (pas de `Tk` sur un serveur sans
écran). On teste ce qui décide : exécution en thread, vidage d'événements,
annulation, exceptions, et câblage de `app.py`.
"""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

from support import make_settings, run, tmpdir  # noqa: I001

import app
from core.agent_core import AgentReply
from core.events import Event, EventBus, EventType
from core.factory import build_agent
from core.llm_client import LLMResponse
from ui.controller import AgentController

PASSPHRASE = "Correct-Horse-Battery-9!"


class _FakeText:
    """Doublure minimale d'un `CTkTextbox` : append-only, comme le vrai widget."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.state = "normal"

    def configure(self, **kwargs) -> None:
        self.state = kwargs.get("state", self.state)

    def insert(self, index: str, text: str) -> None:
        assert index == "end", index
        assert self.state == "normal", "écriture dans un widget désactivé"
        self.chunks.append(text)

    def see(self, index: str) -> None:
        assert index == "end"

    def get(self, start: str, end: str) -> str:
        return "".join(self.chunks)


class _StreamingLLM:
    """Fournisseur qui débite un texte en fragments puis s'arrête."""

    supports_stream = True

    def __init__(self, fragments: list[str]) -> None:
        self.fragments = list(fragments)
        self.received: list[str] = []

    def stream_complete(self, messages, tools=None, *, on_token=None, **kwargs):
        assert on_token is not None, "la boucle doit écouter le flux"
        for fragment in self.fragments:
            self.received.append(fragment)
            on_token(fragment)
        return LLMResponse(text="".join(self.fragments), finish_reason="stop")

    def complete(self, messages, tools=None, **kwargs):  # pragma: no cover - non utilisé
        raise AssertionError("le flux aurait dû être utilisé")


class _ScriptedLLM:
    """Fournisseur piloté par le test, avec temporisation et porte d'attente."""

    def __init__(self, *responses, delay: float = 0.0, gate: threading.Event | None = None):
        self.responses = list(responses)
        self.delay = delay
        self.gate = gate
        self.calls: list[object] = []

    def complete(self, messages, tools=None, **kwargs):
        self.calls.append(tools)
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _bundle(settings, llm):
    return build_agent(settings, llm=llm, with_web=False, with_browser=False)


def test_controller_runs_agent_in_a_thread() -> None:
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        bundle = _bundle(settings, _ScriptedLLM(LLMResponse(text="fini", finish_reason="stop")))
        try:
            controller = AgentController(bundle.agent)
            assert not controller.start("  "), "une tâche vide ne doit pas démarrer"
            assert not controller.busy
            assert controller.start("compte les fichiers")

            reply = controller.wait(10)
            assert reply is not None and reply.ok, reply
            assert reply.text == "fini"
            assert not controller.busy
            assert controller.current is not None and controller.current.finished
            assert controller.current.duration_s >= 0

            events = controller.drain()
            types = [event.type for event in events]
            assert types[:1] == ["run_started"], types
            assert "run_finished" in types
            assert "final" in types
            assert controller.drain() == [], "la file doit être vidée"

            # L'interface laisse une trace JSONL par exécution.
            assert controller.trace.path is not None
            trace_text = controller.trace.path.read_text(encoding="utf-8")
            assert '"type": "run_started"' in trace_text
        finally:
            controller.close()
            bundle.close()


def test_controller_drains_events_from_the_bus_it_was_given() -> None:
    """Le bus fourni à `build_agent` doit être celui de l'agent et du contrôleur."""
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        bus = EventBus()
        bundle = build_agent(
            settings,
            llm=_ScriptedLLM(LLMResponse(text="fini", finish_reason="stop")),
            bus=bus,
            with_web=False,
            with_browser=False,
        )
        try:
            assert bundle.agent.bus is bus, "le bus injecté doit être conservé"
            controller = AgentController(bundle.agent)
            assert controller.start("compte")
            reply = controller.wait(10)
            assert reply is not None and reply.ok

            events = controller.drain()
            types = [event.type for event in events]
            assert types[:1] == [EventType.RUN_STARTED], types
            assert EventType.FINAL in types and EventType.RUN_FINISHED in types
            assert all(event.run_id == bundle.agent.run_id for event in events)
            assert controller.current is not None
            assert controller.current.events == events
        finally:
            bundle.close()


def test_wait_returns_only_once_the_thread_has_ended() -> None:
    """`wait()` garantit `busy == False` : verrouille le correctif de course CI."""
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        llm = _ScriptedLLM(LLMResponse(text="fini", finish_reason="stop"), delay=0.05)
        bundle = _bundle(settings, llm)
        try:
            controller = AgentController(bundle.agent)
            # Le drapeau de fin est posé avant la dernière instruction du
            # thread : on ralentit ce point précis pour que la course soit
            # déterministe, comme sur un runner Windows chargé.
            finished_set = controller._finished.set

            def slow_set() -> None:
                finished_set()
                time.sleep(0.15)

            controller._finished.set = slow_set  # type: ignore[method-assign]

            assert controller.start("tâche lente")
            reply = controller.wait(10)

            assert reply is not None and reply.ok
            assert controller.busy is False, "wait() doit rendre la main thread terminé"
            assert controller._thread is not None and not controller._thread.is_alive()
        finally:
            bundle.close()


def test_controller_refuses_a_second_run() -> None:
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        gate = threading.Event()
        llm = _ScriptedLLM(LLMResponse(text="ok", finish_reason="stop"), gate=gate)
        bundle = _bundle(settings, llm)
        try:
            controller = AgentController(bundle.agent)
            assert controller.start("première")
            assert controller.busy
            assert not controller.start("seconde"), "un seul run à la fois"
            gate.set()
            assert controller.wait(10) is not None
            assert not controller.busy
            assert controller.start("troisième")
            controller.wait(10)
        finally:
            gate.set()
            bundle.close()


def test_controller_cancels_a_running_task() -> None:
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        llm = _ScriptedLLM(LLMResponse(text="trop tard", finish_reason="stop"), delay=0.4)
        bundle = _bundle(settings, llm)
        try:
            controller = AgentController(bundle.agent)
            assert controller.start("tâche longue")
            time.sleep(0.1)
            controller.cancel()
            reply = controller.wait(10)
            assert reply is not None
            assert reply.stop_reason == "cancelled", reply.stop_reason
            assert not reply.ok
        finally:
            bundle.close()


def test_controller_reports_agent_crashes_instead_of_dying() -> None:
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        bundle = _bundle(settings, _ScriptedLLM(RuntimeError("fournisseur en panne")))
        try:
            controller = AgentController(bundle.agent)
            assert controller.start("tâche")
            assert controller.wait(10) is None
            assert controller.current is not None
            assert controller.current.finished
            assert "fournisseur en panne" in controller.current.error
            assert not controller.busy
        finally:
            bundle.close()


def test_gui_module_loads_without_customtkinter() -> None:
    """L'import ne doit pas échouer : c'est `run_gui` qui explique le manque."""
    import ui.app_gui as gui

    assert isinstance(gui.available(), bool)
    if not gui.available():
        try:
            gui.run_gui(None, None)  # type: ignore[arg-type]
        except RuntimeError as exc:
            assert "customtkinter" in str(exc)
        else:  # pragma: no cover - dépendance présente
            raise AssertionError("run_gui aurait dû refuser de démarrer")


def test_cli_check_and_headless_task() -> None:
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        saved = settings.save()

        assert app.build_parser().parse_args([]).task is None
        assert app.main(["--config", str(saved), "--check"]) in {0, 1}

        class _FakeBundle:
            def __init__(self) -> None:
                self.closed = False
                self.agent = self._Agent()
                self.vault = self._Vault()

            class _Agent:
                bus = EventBus()

                def run(self, task):
                    assert task == "résume la note"
                    from core.agent_core import AgentReply

                    return AgentReply(text="résumé", steps=[], stop_reason="final")

            class _Vault:
                def unlock_from_keyring(self) -> bool:
                    return False

            def close(self) -> None:
                self.closed = True

        fake = _FakeBundle()
        original = app.build_agent
        app.build_agent = lambda *a, **k: fake  # type: ignore[assignment]
        try:
            code = app.main(["--config", str(saved), "--task", "résume la note"])
        finally:
            app.build_agent = original

        assert code == 0, code
        assert fake.closed, "le bundle doit être fermé après la tâche"


def test_streamed_fragments_arrive_in_order_without_overwriting() -> None:
    with tmpdir("ui") as tmp:
        settings = make_settings(tmp)
        settings.llm.stream = True
        fragments = ["Voici ", "le ", "résumé", " demandé."]
        llm = _StreamingLLM(fragments)
        bundle = _bundle(settings, llm)
        try:
            controller = AgentController(bundle.agent)
            assert controller.start("résume")
            reply = controller.wait(10)
            assert reply is not None and reply.ok and reply.text == "".join(fragments)
            assert controller.current is not None
            assert controller.current.streamed == "".join(fragments)

            events = controller.drain()
            chunks = [
                event.payload["text"] for event in events if event.type == "stream_chunk"
            ]
            assert chunks == fragments, chunks
            assert events.index(next(e for e in events if e.type == "stream_chunk")) < events.index(
                next(e for e in events if e.type == "final")
            ), "les fragments précèdent la réponse finale"
        finally:
            bundle.close()


        # L'écriture dans la transcription ne remplace jamais le texte existant.
        from ui.app_gui import TranscriptBuffer

        widget = _FakeText()
        buffer = TranscriptBuffer(widget)
        buffer.write("Vous résume\n\nAgent ")
        for fragment in fragments:
            buffer.write(fragment)
        assert buffer.text() == "Vous résume\n\nAgent " + "".join(fragments)
        assert len(widget.chunks) == 1 + len(fragments)
        assert widget.state == "disabled"


def test_maintenance_report_lists_every_directory() -> None:
    from ui.app_gui import format_maintenance_report

    text = format_maintenance_report(
        [
            {"directory": "C:/runs", "action": "compact", "runs": 2},
            {"directory": "C:/runs/atelier", "action": "rebuild", "runs": 0},
            {"directory": "C:/runs/vide", "action": "unchanged", "runs": 0},
        ]
    )

    lines = text.splitlines()
    assert lines[0] == "compacté · 2 run(s) · C:/runs"
    assert lines[1] == "reconstruit · 0 run(s) · C:/runs/atelier"
    assert lines[2] == "déjà à jour · 0 run(s) · C:/runs/vide"
    assert format_maintenance_report([]) == "Aucun index à entretenir."


def test_retry_after_a_cut_discards_the_abandoned_fragment() -> None:
    """Un flux coupé n'affiche rien : son fragment n'a jamais été libéré."""
    from ui.app_gui import OmniAgentWindow, TranscriptBuffer

    class _Button:
        def configure(self, **kwargs) -> None:
            pass

    class _Controller:
        def __init__(self, events: list[Event], reply: AgentReply) -> None:
            self._events = list(events)
            self.reply = reply
            self.busy = False

        def drain(self) -> list[Event]:
            events, self._events = self._events, []
            return events

    widget = _FakeText()
    # Pas de Tk ici : seule la logique de transcription de `_pump` est testée.
    window = object.__new__(OmniAgentWindow)
    window._buffer = TranscriptBuffer(widget)
    window._step = 0
    window._streaming = False
    window._reported = False
    window._pending = {}
    window._set_status = lambda text: None
    window.after = lambda _ms, _callback: "tick"
    window.send_button = _Button()
    window.cancel_button = _Button()
    window.settings = SimpleNamespace(ui=SimpleNamespace(stream_refresh_ms=50))
    window.controller = _Controller(
        [
            Event(type=EventType.STREAM_CHUNK, payload={"text": "Bonjour"}),
            Event(type=EventType.LLM_RETRY, payload={"attempt": 1, "code": "llm_error"}),
            Event(type=EventType.STREAM_CHUNK, payload={"text": "Salut"}),
            Event(type=EventType.FINAL, payload={"text": "Salut"}),
        ],
        AgentReply(text="Salut", steps=[], stop_reason="final"),
    )

    window._pump()

    # Le fragment abandonné n'a jamais été écrit : seule la tentative qui
    # conclut apparaît, précédée de la note de reprise.
    assert widget.get("1.0", "end") == (
        "• reprise 1 après llm_error\n\n"
        "Agent Salut\n"
    )
    assert window._streaming is False, "le drapeau doit être retombé après la réponse"


def test_retry_closes_a_displayed_attempt_before_the_next_one() -> None:
    """Un flux déjà affiché est refermé : la tentative suivante ne s'y colle pas."""
    from ui.app_gui import OmniAgentWindow, TranscriptBuffer

    class _Button:
        def configure(self, **kwargs) -> None:
            pass

    class _Controller:
        def __init__(self, events: list[Event], reply: AgentReply) -> None:
            self._events = list(events)
            self.reply = reply
            self.busy = False

        def drain(self) -> list[Event]:
            events, self._events = self._events, []
            return events

    widget = _FakeText()
    window = object.__new__(OmniAgentWindow)
    window._buffer = TranscriptBuffer(widget)
    window._step = 0
    window._streaming = False
    window._reported = False
    window._pending = {}
    window._set_status = lambda text: None
    window.after = lambda _ms, _callback: "tick"
    window.send_button = _Button()
    window.cancel_button = _Button()
    window.settings = SimpleNamespace(ui=SimpleNamespace(stream_refresh_ms=50))
    window.controller = _Controller(
        [
            # Étape 1 : la réponse a commencé à s'afficher…
            Event(type=EventType.STREAM_CHUNK, payload={"step": 1, "text": "Bonjour"}),
            Event(type=EventType.FINAL, payload={"step": 1, "text": "Bonjour"}),
            # … puis le flux est coupé et repris.
            Event(type=EventType.LLM_RETRY, payload={"attempt": 1, "code": "llm_error"}),
            Event(type=EventType.STREAM_CHUNK, payload={"step": 2, "text": "Salut"}),
            Event(type=EventType.FINAL, payload={"step": 2, "text": "Salut"}),
        ],
        AgentReply(text="Salut", steps=[], stop_reason="final"),
    )

    window._pump()

    text = widget.get("1.0", "end")
    assert text == (
        "Agent Bonjour\n"
        "• fragment interrompu\n\n"
        "• reprise 1 après llm_error\n\n"
        "Agent Salut\n"
    ), text
    assert "BonjourSalut" not in text, "le fragment abandonné s'est collé à la réponse"
    assert window._streaming is False, "le drapeau doit être retombé"


def test_context_trim_report_is_written_in_the_transcript() -> None:
    """L'utilisateur doit voir que le contexte a fondu, et pourquoi."""
    from ui.app_gui import OmniAgentWindow, TranscriptBuffer

    class _Button:
        def configure(self, **kwargs) -> None:
            pass

    class _Controller:
        def __init__(self) -> None:
            self._events = [
                Event(
                    type=EventType.CONTEXT_TRIMMED,
                    payload={
                        "before_messages": 12,
                        "after_messages": 7,
                        "before_tokens": 900,
                        "after_tokens": 420,
                        "summarized": 5,
                    },
                )
            ]
            self.reply = None
            self.busy = False

        def drain(self) -> list[Event]:
            events, self._events = self._events, []
            return events

    widget = _FakeText()
    window = object.__new__(OmniAgentWindow)
    window._buffer = TranscriptBuffer(widget)
    window._step = 0
    window._streaming = False
    window._reported = False
    window._pending = {}
    window._set_status = lambda text: None
    window.after = lambda _ms, _callback: "tick"
    window.send_button = _Button()
    window.cancel_button = _Button()
    window.settings = SimpleNamespace(ui=SimpleNamespace(stream_refresh_ms=50))
    window.controller = _Controller()

    window._pump()

    text = widget.get("1.0", "end")
    assert "contexte replié" in text
    assert "12 -> 7 messages" in text
    assert "900 -> 420 tokens" in text
    assert "5 résumé(s)" in text


def test_approval_dialog_uses_explicit_labels_and_refuses_by_default() -> None:
    """Les trois choix explicites sont seuls acceptés ; l'absence vaut refus."""
    from ui import approval_dialog

    assert [label for _decision, label in approval_dialog.APPROVAL_CHOICES] == [
        "Autoriser une fois",
        "Autoriser pour la session",
        "Refuser",
    ]
    assert approval_dialog.DEFAULT_DECISION == "deny"
    assert approval_dialog.APPROVAL_CHOICES[-1] == ("deny", "Refuser")
    assert approval_dialog.decision_for("once") == "once"
    assert approval_dialog.decision_for("session") == "session"
    assert approval_dialog.decision_for("deny") == "deny"
    assert approval_dialog.decision_for(None) == "deny"
    assert approval_dialog.decision_for("inconnu") == "deny"
    assert approval_dialog.dialog_title("fs_delete") == (
        "Confirmation requise - fs_delete"
    )


def test_approval_dialog_close_is_a_refusal() -> None:
    """Fermer la fenêtre (X ou Échap) emprunte toujours la décision Refuser."""
    from ui.approval_dialog import ApprovalDialog

    decisions: list[str] = []
    dialog = object.__new__(ApprovalDialog)
    dialog._choose = decisions.append

    dialog._refuse()

    assert decisions == ["deny"]


def test_approval_dialog_delegates_to_explicit_choices(monkeypatch) -> None:
    """La fenêtre reçoit la demande et retourne once/session/deny tel quel."""
    from ui.app_gui import OmniAgentWindow

    request = SimpleNamespace(tool="fs_delete", preview="Suppression de C:\\tmp\\x")
    seen: list[tuple[object, object]] = []
    answers = ["once", "session", "deny"]

    def ask(parent: object, received: object) -> str:
        seen.append((parent, received))
        return answers.pop(0)

    monkeypatch.setattr("ui.approval_dialog.ask_tool_approval", ask)
    window = object.__new__(OmniAgentWindow)
    window.after = lambda _ms, callback: callback()

    assert window._tool_approval(request) == "once"
    assert window._tool_approval(request) == "session"
    assert window._tool_approval(request) == "deny"
    assert len(seen) == 3
    assert all(parent is window and received is request for parent, received in seen)


def test_close_stream_lowers_the_flag_even_without_a_displayed_attempt() -> None:
    """`_close_stream` referme la tentative : le drapeau ne reste jamais vrai."""
    from ui.app_gui import OmniAgentWindow, TranscriptBuffer

    widget = _FakeText()
    window = object.__new__(OmniAgentWindow)
    window._buffer = TranscriptBuffer(widget)
    window._streaming = True

    window._close_stream("flux interrompu")

    assert window._streaming is False
    assert "flux interrompu" in widget.get("1.0", "end")


def test_turn_that_calls_a_tool_keeps_its_text_hidden() -> None:
    """Comme en console : le préambule d'une étape qui agit ne s'affiche pas."""
    from ui.app_gui import OmniAgentWindow, TranscriptBuffer

    class _Button:
        def configure(self, **kwargs) -> None:
            pass

    class _Controller:
        def __init__(self) -> None:
            self._events: list[Event] = []
            self.reply: AgentReply | None = None
            self.busy = False

        def push(self, *events: Event) -> None:
            self._events.extend(events)

        def drain(self) -> list[Event]:
            events, self._events = self._events, []
            return events

    widget = _FakeText()
    # Pas de Tk ici : seule la logique de transcription de `_pump` est testée.
    window = object.__new__(OmniAgentWindow)
    window._buffer = TranscriptBuffer(widget)
    window._step = 0
    window._streaming = False
    window._reported = False
    window._pending = {}
    window._set_status = lambda text: None
    window.after = lambda _ms, _callback: "tick"
    window.send_button = _Button()
    window.cancel_button = _Button()
    window.settings = SimpleNamespace(ui=SimpleNamespace(stream_refresh_ms=50))
    window.controller = controller = _Controller()

    # Étape 1 : le modèle annonce qu'il va lire, puis appelle l'outil.
    controller.push(
        Event(
            type=EventType.STREAM_CHUNK,
            payload={"step": 1, "text": "Je vais lire le fichier."},
        ),
        Event(type=EventType.TOOL_STARTED, payload={"step": 1, "tool": "fs_read_text"}),
    )
    window._pump()
    assert "Je vais lire" not in widget.get("1.0", "end")
    assert "lecture d'un fichier" in widget.get("1.0", "end")

    # Étape 2 : le texte qui conclut est libéré, fragment par fragment.
    controller.push(
        Event(type=EventType.STREAM_CHUNK, payload={"step": 2, "text": "Voici la "}),
        Event(type=EventType.STREAM_CHUNK, payload={"step": 2, "text": "réponse."}),
        Event(type=EventType.FINAL, payload={"step": 2, "text": "Voici la réponse."}),
    )
    controller.reply = AgentReply(text="Voici la réponse.", steps=[], stop_reason="final")
    window._pump()

    assert widget.get("1.0", "end") == (
        "  · lecture d'un fichier\n\n"
        "Agent Voici la réponse.\n"
    ), widget.get("1.0", "end")
    assert window._pending == {}, "le tampon doit être vidé après la réponse"


def _approved_window(settings, reply, *, run_id: str = "run-42"):
    """Fenêtre minimale (sans Tk) avec une proposition approuvée en attente."""
    from ui.app_gui import OmniAgentWindow, TranscriptBuffer

    class _Button:
        def configure(self, **kwargs) -> None:
            pass

    class _Agent:
        pass

    class _Controller:
        def __init__(self) -> None:
            self.reply = reply
            self.busy = False
            self.current = SimpleNamespace(finished=True, error="panne")
            self.agent = _Agent()
            self.agent.run_id = run_id

        def drain(self) -> list[Event]:
            return []

    widget = _FakeText()
    window = object.__new__(OmniAgentWindow)
    window._buffer = TranscriptBuffer(widget)
    window._step = 0
    window._streaming = False
    window._reported = False
    window._pending = {}
    window._approved_proposal = "prop-1"
    window._set_status = lambda text: None
    window.after = lambda _ms, _callback: "tick"
    window.send_button = _Button()
    window.cancel_button = _Button()
    window.settings = settings
    window.controller = _Controller()
    return window, widget


def test_approved_proposal_is_marked_executed_with_its_run_id() -> None:
    from core.proposals import ProposalStore, proposals_path

    with tmpdir("ui-approbation") as tmp:
        settings = make_settings(tmp)
        store = ProposalStore(proposals_path(settings))
        store.append(
            {
                "type": "proposal",
                "id": "prop-1",
                "state": "approved",
                "task": "Ranger les rapports",
            }
        )
        window, widget = _approved_window(
            settings,
            AgentReply(text="rangé", steps=[], stop_reason="final", ok=True),
        )

        window._pump()

        record = store.find("prop-1")
        assert record["state"] == "executed"
        assert record["run_id"] == "run-42"
        assert record["ok"] is True
        assert "interface" in record["detail"]
        assert window._approved_proposal is None, "la clôture ne doit pas se répéter"
        assert "proposition prop-1 : executed" in widget.get("1.0", "end")


def test_approved_proposal_is_marked_failed_when_the_run_fails() -> None:
    from core.proposals import ProposalStore, proposals_path

    with tmpdir("ui-approbation-echec") as tmp:
        settings = make_settings(tmp)
        store = ProposalStore(proposals_path(settings))
        store.append(
            {
                "type": "proposal",
                "id": "prop-1",
                "state": "approved",
                "task": "Ranger les rapports",
            }
        )
        window, _widget = _approved_window(
            settings,
            AgentReply(
                text="annulé",
                steps=[],
                stop_reason="cancelled",
                ok=False,
                error="arrêt demandé",
            ),
            run_id="run-7",
        )

        window._pump()

        record = store.find("prop-1")
        assert record["state"] == "failed"
        assert record["run_id"] == "run-7"
        assert record["ok"] is False
        assert "cancelled" in record["detail"]
        assert window._approved_proposal is None


if __name__ == "__main__":
    sys.exit(run(globals()))
