"""Fenêtre CustomTkinter de l'agent.

Le module s'importe même sans customtkinter installé : `available()` renvoie
alors False et `run_gui()` explique quoi installer. Rien de la logique de
l'agent ne vit ici — la fenêtre ne fait qu'afficher les événements du
`AgentController` et transmettre les actions de l'utilisateur.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from config import APP_ID, APP_NAME, APP_VERSION, Settings
from core.events import Event, EventType
from core.factory import AgentBundle
from ui.controller import AgentController

try:  # pragma: no cover - dépend de l'environnement
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None  # type: ignore[assignment]

__all__ = ["OmniAgentWindow", "available", "run_gui"]

logger = logging.getLogger(f"{APP_ID}.ui")

_TOOL_LABELS = {
    "fs_read_text": "lecture d'un fichier",
    "fs_write_text": "écriture d'un fichier",
    "web_search": "recherche web",
    "web_fetch": "lecture d'une page",
    "browser_open": "ouverture du navigateur",
    "vault_create_credential": "création d'un identifiant",
}


def available() -> bool:
    """Vrai si customtkinter est installé."""
    return ctk is not None


class TranscriptBuffer:
    """Écriture incrémentale dans un widget texte (`insert`/`get` de Tk).

    Les fragments sont toujours ajoutés à la fin : jamais de remplacement, donc
    jamais de texte déjà affiché écrasé par le suivant. Le widget est passé en
    paramètre, ce qui permet de tester l'écriture sans afficheur.
    """

    def __init__(self, widget: Any) -> None:
        self.widget = widget

    def write(self, text: str) -> None:
        if not text:
            return
        self.widget.configure(state="normal")
        self.widget.insert("end", text)
        self.widget.see("end")
        self.widget.configure(state="disabled")

    def text(self) -> str:
        return str(self.widget.get("1.0", "end"))


def _missing_dependency_message() -> str:
    return (
        "customtkinter est absent : l'interface graphique ne peut pas démarrer.\n"
        "Installez les dépendances avec « pip install -r requirements.txt », "
        "ou lancez une tâche sans interface : python app.py --task \"…\""
    )


if ctk is not None:  # pragma: no branch - une seule branche est définie

    class OmniAgentWindow(ctk.CTk):  # type: ignore[misc, name-defined]
        """Fenêtre principale : conversation, journal d'outils, statut."""

        #: Défaut de classe : les tests qui construisent la fenêtre sans Tk
        #: (`object.__new__`) n'ont pas cette file, et `getattr` sur un objet Tk
        #: sans attribut déclenche sa recherche dynamique récursive.
        _autonomy_messages: queue.Queue[str] | None = None

        def __init__(self, settings: Settings, bundle: AgentBundle) -> None:
            ctk.set_appearance_mode(settings.ui.appearance)
            ctk.set_default_color_theme("blue")
            super().__init__()

            self.settings = settings
            self.bundle = bundle
            self.controller = AgentController(bundle.agent)
            self._pump_id: str | None = None
            self._step = 0
            self._reported = False
            self._streaming = False
            # Session autonome : le thread de travail n'écrit jamais dans Tk, il
            # dépose ses nouvelles dans cette file, vidée par `_pump`.
            self._autonomy_thread: threading.Thread | None = None
            self._autonomy_session: Any = None
            self._autonomy_messages: queue.Queue[str] = queue.Queue()
            # Fragments en attente, par étape : un tour qui appelle un outil ne
            # s'affiche pas, seul le tour qui conclut est libéré (règle identique
            # à `_ConsoleStream` en mode `--task`).
            self._pending: dict[int, list[str]] = {}

            self.title(f"{APP_NAME} {APP_VERSION}")
            self.geometry(f"{settings.ui.window_width}x{settings.ui.window_height}")
            self.minsize(900, 620)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            self._build_widgets()
            self._append("système", "Prêt. Décrivez une tâche, Ctrl+Entrée pour envoyer.")
            self._pump()
            # Après la boucle : l'assistant s'ouvre au-dessus de la fenêtre déjà
            # construite, ce qui évite un second interpréteur Tk (deux `Tk()`
            # successifs cassent les images de CustomTkinter).
            self.after(150, self._first_run_check)

        # -- construction ---------------------------------------------------

        def _build_widgets(self) -> None:
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(1, weight=1)

            header = ctk.CTkFrame(self)
            header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
            header.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                header,
                text=f"{APP_NAME} — assistant autonome Windows",
                font=(self.settings.ui.font_family, self.settings.ui.font_size + 3, "bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
            self.status = ctk.CTkLabel(
                header, text="prêt", anchor="w", text_color="#9CA3AF"
            )
            self.status.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
            ctk.CTkButton(
                header, text="Assistant", width=110, command=self._open_assistant
            ).grid(row=0, column=1, rowspan=2, padx=10, pady=8)

            self.transcript = ctk.CTkTextbox(self, wrap="word", state="disabled")
            self.transcript.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
            self._buffer = TranscriptBuffer(self.transcript)

            bottom = ctk.CTkFrame(self)
            bottom.grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 12))
            bottom.grid_columnconfigure(0, weight=1)
            self.prompt = ctk.CTkTextbox(bottom, height=90, wrap="word")
            self.prompt.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=6)
            self.prompt.bind("<Control-Return>", lambda _event: self._send())

            actions = ctk.CTkFrame(bottom)
            actions.grid(row=0, column=1, sticky="ns", pady=6)
            self.send_button = ctk.CTkButton(actions, text="Envoyer", command=self._send)
            self.send_button.pack(fill="x", pady=(0, 6))
            self.cancel_button = ctk.CTkButton(
                actions, text="Annuler", command=self._cancel, state="disabled"
            )
            self.cancel_button.pack(fill="x")
            ctk.CTkButton(
                actions, text="Traces", width=110, command=self._open_traces
            ).pack(fill="x", pady=(6, 0))
            self.autonomy_button = ctk.CTkButton(
                actions, text="Autonomie", width=110, command=self._toggle_autonomy
            )
            self.autonomy_button.pack(fill="x", pady=(6, 0))
            # Trace sur disque : l'utilisateur doit pouvoir refuser qu'une
            # session laisse la tâche et les réponses en clair sur la machine.
            self.trace_switch = ctk.CTkSwitch(
                actions, text="trace", command=self._toggle_trace
            )
            self.trace_switch.pack(fill="x", pady=(6, 0))
            if self.controller.tracing:
                self.trace_switch.select()
            else:
                self.trace_switch.deselect()

        # -- affichage ------------------------------------------------------

        def _append(self, kind: str, text: str) -> None:
            body = str(text).rstrip()
            if not body:
                return
            prefix = {
                "vous": "Vous",
                "agent": "Agent",
                "outil": "  ·",
                "erreur": "!",
                "système": "•",
            }.get(kind, "")
            self._buffer.write(f"{prefix} {body}\n\n" if prefix else f"{body}\n")

        def _set_status(self, text: str) -> None:
            self.status.configure(text=text)

        def _close_stream(self, reason: str = "fragment interrompu") -> None:
            """Clôt explicitement la tentative en cours.

            Appelée sur reprise ou sur erreur. Le drapeau retombe **toujours** :
            une tentative abandonnée ne doit pas laisser croire que du texte est
            encore en cours. Si des fragments avaient commencé à s'afficher, une
            ligne dédiée referme le paragraphe, pour que la tentative suivante
            reparte sur son propre en-tête au lieu de se coller à ses derniers
            mots.
            """
            was_streaming = self._streaming
            self._streaming = False
            if was_streaming:
                self._buffer.write(f"\n• {reason}\n\n")

        # -- actions --------------------------------------------------------

        def _first_run_check(self) -> None:
            """Ouvre l'assistant si l'application ne peut pas encore générer."""
            try:
                from ui.setup_wizard import needs_setup

                if not needs_setup(self.settings):
                    return
            except Exception as exc:  # noqa: BLE001 - jamais bloquant
                logger.exception("Assistant de démarrage indisponible")
                self._append("erreur", f"assistant indisponible : {exc}")
                return
            self._append(
                "système",
                "premier démarrage : la clé API manque. L'assistant s'ouvre pour la "
                "renseigner, avec le dossier de travail, le navigateur et le coffre.",
            )
            self._open_assistant()

        def _open_assistant(self) -> None:
            """Rouvre l'assistant de premier démarrage avec les valeurs actuelles."""
            try:
                from ui.setup_wizard import run_setup

                saved = run_setup(self.settings, parent=self)
            except Exception as exc:  # noqa: BLE001 - l'interface reste utilisable
                logger.exception("Assistant de démarrage en échec")
                self._append("erreur", f"assistant indisponible : {exc}")
                return

            if saved:
                self._append(
                    "système",
                    "assistant : réglages enregistrés — clé active tout de suite, "
                    "dossier et canal au prochain démarrage",
                )
            else:
                self._set_status("assistant fermé sans modification")

        def _send(self) -> None:
            if self._autonomy_thread is not None and self._autonomy_thread.is_alive():
                self._set_status("session autonome en cours : arrêtez-la d'abord")
                return
            task = self.prompt.get("1.0", "end").strip()
            if not task:
                return
            if not self.controller.start(task):
                self._set_status("une tâche est déjà en cours")
                return
            self.prompt.delete("1.0", "end")
            self._step = 0
            self._reported = False
            self._streaming = False
            self._pending.clear()
            self._append("vous", task)
            self._set_status("en cours…")
            self.send_button.configure(state="disabled")
            self.cancel_button.configure(state="normal")

        def _cancel(self) -> None:
            self.controller.cancel()
            self._set_status("arrêt demandé…")

        def _toggle_trace(self) -> None:
            enabled = bool(self.trace_switch.get())
            self.controller.set_tracing(enabled)
            self._set_status(
                "trace active"
                if enabled
                else "trace désactivée : rien n'est écrit sur disque"
            )

        def _toggle_autonomy(self) -> None:
            """Démarre ou arrête une session autonome, sans quitter l'interface."""
            thread = self._autonomy_thread
            if thread is not None and thread.is_alive():
                session = self._autonomy_session
                if session is not None:
                    session.request_stop()
                self._set_status("autonomie : arrêt demandé")
                return

            from core.autonomy import AutonomySession, goals_path, read_goals

            goals = goals_path(self.settings)
            if not read_goals(goals):
                self._append(
                    "système",
                    f"autonomie : renseignez {goals} avant de lancer une session",
                )
                return
            if self.controller.busy:
                self._set_status("une tâche est déjà en cours")
                return

            session = AutonomySession(self.settings, self.controller.agent)
            self._autonomy_session = session
            self.autonomy_button.configure(text="Arrêter")
            self._append(
                "système",
                f"autonomie : {self.settings.autonomy.max_cycles} cycle(s), "
                f"toutes les {self.settings.autonomy.interval_minutes:g} min",
            )
            self._set_status("autonomie en cours…")
            self._autonomy_thread = threading.Thread(
                target=self._autonomy_loop,
                args=(session,),
                name="omniagent-autonomy",
                daemon=True,
            )
            self._autonomy_thread.start()

        def _autonomy_loop(self, session: Any) -> None:
            """Thread de la session : ne touche jamais aux widgets directement."""

            def report(record: dict) -> None:
                if record.get("type") == "no_goals":
                    message = "autonomie : aucun objectif, session terminée"
                else:
                    status = (
                        "ok"
                        if record.get("ok")
                        else f"échec ({record.get('stop_reason') or '?'})"
                    )
                    message = f"autonomie, cycle {record.get('cycle')} : {status}"
                    excerpt = str(record.get("excerpt") or "")
                    if excerpt:
                        message += f" — {excerpt[:200]}"
                self._autonomy_messages.put(message)

            try:
                session.run(on_cycle=report)
            except Exception as exc:  # noqa: BLE001 - l'interface doit rester vivante
                logger.exception("session autonome interrompue")
                self._autonomy_messages.put(f"autonomie interrompue : {exc}")
            finally:
                self._autonomy_messages.put("__autonomy_done__")

        def _open_traces(self) -> None:
            """Liste les exécutions enregistrées et en relit une à la demande."""
            directory = self.settings.paths.log_dir / "runs"
            window = ctk.CTkToplevel(self)
            window.title("Traces d'exécution")
            window.geometry("760x480")
            window.transient(self)
            frame = ctk.CTkScrollableFrame(window, label_text=str(directory))
            frame.pack(fill="both", expand=True, padx=10, pady=10)
            # L'index est un cache : absent ou illisible, la liste reste possible.
            states: dict[str, str] = {}
            if directory.is_dir():
                try:
                    from core.run_index import RunIndex

                    states = {
                        run_id: record.state
                        for run_id, record in RunIndex(directory).entries().items()
                    }
                except Exception:  # noqa: BLE001 - un cache ne bloque pas l'interface
                    logger.debug("index des runs illisible", exc_info=True)
            files: list[Any] = []
            if directory.is_dir():
                try:
                    # Sous-dossiers (rétention par espace de travail) et archives
                    # compris : une trace comprimée reste lisible par le rejeu.
                    files = sorted(
                        (
                            item
                            for item in directory.rglob("*")
                            if item.is_file()
                            and (
                                item.name.endswith(".jsonl")
                                or item.name.endswith(".jsonl.gz")
                            )
                        ),
                        key=lambda item: item.stat().st_mtime,
                        reverse=True,
                    )[:50]
                except OSError as exc:
                    self._append("erreur", f"dossier de traces illisible : {exc}")
            if not files:
                ctk.CTkLabel(frame, text="Aucune trace enregistrée.").pack(pady=20)
                return
            for path in files:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                row = ctk.CTkFrame(frame)
                row.pack(fill="x", pady=2)
                stamp = time.strftime("%d/%m %H:%M", time.localtime(stat.st_mtime))
                stem = path.name[:-3] if path.name.endswith(".gz") else path.name
                stem = stem[:-6] if stem.endswith(".jsonl") else stem
                state = states.get(stem, "")
                label = f"{stem}   {stamp}   {max(1, stat.st_size // 1024)} Kio"
                if state:
                    label += f"   [{state}]"
                ctk.CTkLabel(row, text=label, anchor="w").pack(
                    side="left", padx=8, fill="x", expand=True
                )
                ctk.CTkButton(
                    row, text="Lire", width=70, command=lambda p=path: self._show_trace(p)
                ).pack(side="right", padx=6, pady=4)

        def _show_trace(self, path: Any) -> None:
            """Rejoue une trace dans la conversation, sans relancer l'agent."""
            from core.replay import format_run, replay

            key = None
            trace_cfg = self.controller.agent.settings.trace
            if trace_cfg.encrypt:
                from core.trace_crypto import TraceKeyError, load_key

                try:
                    key = load_key(trace_cfg.key_env)
                except TraceKeyError as exc:
                    self._append("erreur", f"trace chiffrée : {exc}")
                    return
            try:
                run = replay(path, key=key)
            except Exception as exc:  # noqa: BLE001 - une trace cassée ne doit rien casser
                self._append("erreur", f"trace illisible : {exc}")
                return
            self._buffer.write(format_run(run) + "\n\n")
            self._set_status(f"trace relue : {path.stem}")

        def _pump(self) -> None:
            for event in self.controller.drain():
                self._render(event)

            # Après les événements : « terminée » ne doit pas être écrasé par le
            # dernier « étape n/… » émis juste avant la fin de la session.
            messages = getattr(self, "_autonomy_messages", None)
            while messages is not None:
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    break
                if message == "__autonomy_done__":
                    self.autonomy_button.configure(text="Autonomie")
                    self._set_status("autonomie terminée")
                else:
                    self._append("système", message)

            reply = self.controller.reply
            if reply is not None and not self.controller.busy and not self._reported:
                self._reported = True
                if reply.ok:
                    if self._streaming:
                        # Le texte est déjà affiché fragment par fragment.
                        self._buffer.write("\n")
                    elif reply.stop_reason == "final" and reply.text:
                        self._append("agent", reply.text)
                    elif not reply.text:
                        self._append("agent", "(aucune réponse)")
                else:
                    self._close_stream("flux interrompu")
                    self._append("erreur", reply.text or reply.error)
                self._streaming = False
                self._pending.clear()

                status = "prêt" if reply.ok else f"arrêté ({reply.stop_reason})"
                if reply.tool_calls:
                    status += f" · {reply.tool_calls} outil(s)"
                if reply.recoveries:
                    status += f" · {reply.recoveries} reprise(s)"
                # `getattr` : l'interface doit rester utilisable avec un
                # contrôleur de test qui n'expose pas de trace.
                trace = getattr(self.controller, "trace", None)
                if trace is not None and trace.truncated:
                    # Une trace incomplète ne doit pas ressembler à un run propre.
                    self._append(
                        "système",
                        "trace incomplète : plafond de taille atteint, la fin de "
                        "l'exécution n'est pas enregistrée",
                    )
                    status += " · trace tronquée"
                self._set_status(status)
                self.send_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")

            interval = max(30, int(self.settings.ui.stream_refresh_ms))
            self._pump_id = self.after(interval, self._pump)

        def _render(self, event: Event) -> None:
            payload: dict[str, Any] = event.payload if isinstance(event.payload, dict) else {}
            if event.type == EventType.STEP_STARTED:
                self._step = int(payload.get("index", self._step + 1) or 1)
                self._set_status(
                    f"étape {self._step}/{payload.get('max_steps', '?')} — réflexion…"
                )
            elif event.type == EventType.TOOL_STARTED:
                # L'étape agit : son texte était un préambule, pas une réponse.
                self._pending.pop(int(payload.get("step") or 0), None)
                tool = str(payload.get("tool", "outil"))
                self._append("outil", _TOOL_LABELS.get(tool, tool))
            elif event.type == EventType.TOOL_FINISHED:
                if not payload.get("ok", True):
                    code = payload.get("error_code") or "échec"
                    self._append("outil", f"{payload.get('tool', 'outil')} : {code}")
            elif event.type == EventType.STREAM_CHUNK:
                # Rien n'est écrit tout de suite : impossible de savoir si ce
                # tour conclut ou s'il annonce un appel d'outil.
                chunk = str(payload.get("text", ""))
                if chunk:
                    step = int(payload.get("step") or 0)
                    self._pending.setdefault(step, []).append(chunk)
            elif event.type == EventType.FINAL:
                # L'étape conclut : son texte est la réponse, on le libère
                # fragment par fragment, dans l'ordre où il est arrivé.
                step = int(payload.get("step") or 0)
                chunks = self._pending.pop(step, [])
                if chunks:
                    self._buffer.write("Agent ")
                    for chunk in chunks:
                        self._buffer.write(chunk)
                    self._streaming = True
            elif event.type == EventType.LLM_RETRY:
                # Le fragment abandonné n'a jamais été affiché : il est jeté, et
                # la tentative suivante repart sur son propre en-tête « Agent ».
                self._pending.clear()
                self._close_stream()
                self._append(
                    "système",
                    f"reprise {payload.get('attempt', '?')} après {payload.get('code', 'erreur')}",
                )
            elif event.type == EventType.CONTEXT_TRIMMED:
                # L'agent « oublie » volontairement : le dire évite de passer
                # pour une panne de mémoire.
                self._append(
                    "système",
                    f"contexte replié : {payload.get('before_messages', '?')} → "
                    f"{payload.get('after_messages', '?')} messages "
                    f"(~{payload.get('before_tokens', '?')} → "
                    f"{payload.get('after_tokens', '?')} tokens, "
                    f"{payload.get('summarized', 0)} résumé(s))",
                )
            elif event.type == EventType.ERROR:
                self._append("erreur", str(payload.get("message", "erreur inconnue")))

        # -- fermeture ------------------------------------------------------

        def _on_close(self) -> None:
            try:
                if self._autonomy_session is not None:
                    self._autonomy_session.request_stop()
                if self.controller.busy:
                    self.controller.cancel()
                self.controller.close()
                if self._pump_id is not None:
                    self.after_cancel(self._pump_id)
            finally:
                self.destroy()

else:  # pragma: no cover - environnement sans customtkinter
    OmniAgentWindow = None  # type: ignore[assignment, misc]


def run_gui(settings: Settings, bundle: AgentBundle) -> int:
    """Lance la boucle graphique ; retourne un code de sortie."""
    if ctk is None or OmniAgentWindow is None:  # pragma: no cover - dépendance
        raise RuntimeError(_missing_dependency_message())
    window = OmniAgentWindow(settings, bundle)
    try:
        window.mainloop()
    finally:
        bundle.close()
    return 0
