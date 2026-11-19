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
from ui.hotkey import HotkeyListener

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


def format_autonomy_journal(records: list[dict[str, Any]], *, limit: int = 50) -> str:
    """Rendu lisible des derniers cycles autonomes, du plus récent au plus ancien."""
    blocks: list[str] = []
    for record in reversed(records[-max(1, limit) :]):
        kind = record.get("type")
        stamp = str(record.get("at") or "")[:16].replace("T", " ")
        if kind == "no_goals":
            blocks.append(f"{stamp} · aucun objectif : session arrêtée sans action")
            continue
        if kind != "cycle_finished":
            continue
        status = (
            "ok"
            if record.get("ok")
            else f"échec ({record.get('stop_reason') or '?'})"
        )
        run_id = str(record.get("run_id") or "?")
        excerpt = " ".join(str(record.get("excerpt") or "").split())
        block = f"Cycle {record.get('cycle', '?')} · {stamp} · {status} · run {run_id}"
        if excerpt:
            block += f"\n    {excerpt}"
        if record.get("error"):
            block += f"\n    erreur : {record['error']}"
        blocks.append(block)
    return "\n\n".join(blocks) if blocks else "Aucun cycle autonome enregistré."


def format_maintenance_report(report: list[dict[str, Any]]) -> str:
    """Bilan lisible de `maintain()` : une ligne par dossier traité."""
    if not report:
        return "Aucun index à entretenir."
    labels = {"compact": "compacté", "rebuild": "reconstruit", "unchanged": "déjà à jour"}
    return "\n".join(
        f"{labels.get(str(item.get('action')), item.get('action'))} · "
        f"{item.get('runs', 0)} run(s) · {item.get('directory', '?')}"
        for item in report
    )


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
        #: Défaut de classe pour les fenêtres de test construites sans Tk :
        #: `getattr` sur un objet Tk non initialisé récurserait.
        _approved_proposal: str | None = None
        #: Signal du raccourci global, consomme par `_pump` sur le fil Tk.
        _hotkey_messages: queue.Queue[str] | None = None

        def __init__(self, settings: Settings, bundle: AgentBundle) -> None:
            ctk.set_appearance_mode(settings.ui.appearance)
            ctk.set_default_color_theme("blue")
            super().__init__()

            self.settings = settings
            self.bundle = bundle
            self.controller = AgentController(bundle.agent)
            self._pump_id: str | None = None
            self._step = 0
            #: Dernier run tracé : support du bouton « Annuler ce run ».
            self._last_run_id = ""
            self._reported = False
            self._streaming = False
            # Session autonome : le thread de travail n'écrit jamais dans Tk, il
            # dépose ses nouvelles dans cette file, vidée par `_pump`.
            self._autonomy_thread: threading.Thread | None = None
            self._autonomy_session: Any = None
            self._autonomy_messages: queue.Queue[str] = queue.Queue()
            self._hotkey_messages = queue.Queue()
            self._hotkey = HotkeyListener(settings.ui.emergency_hotkey)
            #: Proposition dont l'exécution a été lancée par le bouton
            #: Approuver : sa clôture (`executed`/`failed`, run_id) attend la
            #: réponse asynchrone de l'agent.
            self._approved_proposal: str | None = None
            # Fragments en attente, par étape : un tour qui appelle un outil ne
            # s'affiche pas, seul le tour qui conclut est libéré (règle identique
            # à `_ConsoleStream` en mode `--task`).
            self._pending: dict[int, list[str]] = {}

            self.title(f"{APP_NAME} {APP_VERSION}")
            self.geometry(f"{settings.ui.window_width}x{settings.ui.window_height}")
            self.minsize(900, 620)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            self._build_widgets()
            if self._hotkey.start(self._global_stop_signal):
                self._append(
                    "système",
                    f"raccourci global {self._hotkey.label} actif (arrêt d'urgence)",
                )
            else:
                self._append(
                    "système",
                    f"raccourci global indisponible : {self._hotkey.error}",
                )
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
            # Retour arrière d'un run : aperçu du journal puis application.
            self.undo_button = ctk.CTkButton(
                actions, text="Annuler ce run", width=110, command=self._undo_last_run
            )
            self.undo_button.pack(fill="x", pady=(6, 0))
            ctk.CTkButton(
                actions, text="Traces", width=110, command=self._open_traces
            ).pack(fill="x", pady=(6, 0))
            self.autonomy_button = ctk.CTkButton(
                actions, text="Autonomie", width=110, command=self._toggle_autonomy
            )
            self.autonomy_button.pack(fill="x", pady=(6, 0))
            # Enregistrement optionnel dans le Planificateur de tâches
            # Windows : la fenêtre permet d'inscrire et de désinscrire.
            self.schedule_button = ctk.CTkButton(
                actions, text="Planificateur", width=110, command=self._open_scheduler
            )
            self.schedule_button.pack(fill="x", pady=(6, 0))
            # Toujours visible, y compris quand une fenêtre modale a le focus :
            # l'arrêt ne doit pas dépendre du bouton « Autonomie » masqué.
            self.emergency_button = ctk.CTkButton(
                actions,
                text="Arrêt d'urgence",
                width=110,
                fg_color="#B91C1C",
                hover_color="#991B1B",
                command=self._emergency_stop,
            )
            self.emergency_button.pack(fill="x", pady=(6, 0))
            self.journal_button = ctk.CTkButton(
                actions, text="Journal", width=110, command=self._open_journal
            )
            self.journal_button.pack(fill="x", pady=(6, 0))
            self.proposals_button = ctk.CTkButton(
                actions, text="Propositions", width=110, command=self._open_proposals
            )
            self.proposals_button.pack(fill="x", pady=(6, 0))
            self.support_button = ctk.CTkButton(
                actions, text="Support", width=110, command=self._support_bundle
            )
            self.support_button.pack(fill="x", pady=(6, 0))
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
            self.bind_all("<Control-Alt-s>", lambda _event: self._emergency_stop())

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

        def _send(self) -> bool:
            if self._autonomy_thread is not None and self._autonomy_thread.is_alive():
                self._set_status("session autonome en cours : arrêtez-la d'abord")
                return False
            task = self.prompt.get("1.0", "end").strip()
            if not task:
                return False
            if not self.controller.start(task):
                self._set_status("une tâche est déjà en cours")
                return False
            self.prompt.delete("1.0", "end")
            self._step = 0
            self._reported = False
            self._streaming = False
            self._pending.clear()
            self._append("vous", task)
            self._set_status("en cours…")
            self.send_button.configure(state="disabled")
            self.cancel_button.configure(state="normal")
            return True

        def _cancel(self) -> None:
            self.controller.cancel()
            self._set_status("arrêt demandé…")

        def _undo_last_run(self) -> None:
            """Aperçu du journal, puis annulation du dernier run après accord."""
            from tkinter import messagebox

            from core.file_tx import undo_run

            run_id = str(getattr(self, "_last_run_id", "") or "")
            if not run_id:
                self._set_status("aucun run à annuler dans cette session")
                return
            preview = undo_run(self.settings, run_id, apply=False)
            if preview is None:
                self._set_status(f"transaction introuvable : {run_id}")
                return
            if not preview.steps:
                self._set_status(f"run {run_id} : aucune écriture à annuler")
                return
            if not messagebox.askyesno(
                "Annuler ce run",
                preview.format()[:1800] + "\n\nAppliquer cette annulation ?",
            ):
                self._set_status("annulation abandonnée (aperçu seulement)")
                return
            report = undo_run(self.settings, run_id, apply=True)
            if report is None:
                self._set_status(f"transaction introuvable : {run_id}")
                return
            kind = "système" if report.ok else "erreur"
            self._append(kind, report.format())
            self._set_status("annulation appliquée" if report.ok else "annulation incomplète")

        def _emergency_stop(self) -> None:
            """Écrit STOP et annule tout run, sans dépendre de la session."""
            from core.stopfile import request_stop, stop_path

            request_stop(self.settings, "interface")
            self.controller.cancel()
            session = self._autonomy_session
            if session is not None:
                session.request_stop()
            self._set_status(f"arrêt d'urgence : {stop_path(self.settings).name} écrit")
            self._append(
                "système",
                f"arrêt d'urgence : {stop_path(self.settings)} "
                "(supprimez ce fichier avant une nouvelle session)",
            )

        def _global_stop_signal(self) -> None:
            """Callback du raccourci global : thread du raccourci, jamais Tk.

            Le fichier STOP et l'annulation sont sûrs depuis n'importe quel
            thread ; l'affichage est confié à `_pump` via une file.
            """
            from core.stopfile import request_stop

            request_stop(self.settings, "raccourci global")
            try:
                self.controller.cancel()
            except Exception:  # noqa: BLE001 - l'arrêt continue
                logger.debug("annulation du run depuis le raccourci impossible", exc_info=True)
            session = self._autonomy_session
            if session is not None:
                try:
                    session.request_stop()
                except Exception:  # noqa: BLE001
                    logger.debug("arrêt de la session autonome impossible", exc_info=True)
            messages = self._hotkey_messages
            if messages is not None:
                messages.put("stop")

        def _toggle_trace(self) -> None:
            enabled = bool(self.trace_switch.get())
            self.controller.set_tracing(enabled)
            self._set_status(
                "trace active"
                if enabled
                else "trace désactivée : rien n'est écrit sur disque"
            )

        # -- Planificateur de tâches Windows --------------------------------

        def _open_scheduler(self) -> None:
            """Fenêtre dédiée : inscrire ou désinscrire la tâche Windows."""
            window = ctk.CTkToplevel(self)
            window.title("Planificateur de tâches Windows")
            window.geometry("560x300")
            ctk.CTkLabel(
                window,
                text=(
                    "Inscription facultative dans le Planificateur de tâches "
                    "Windows.\n\n"
                    "L'application sera lancée à l'ouverture de session et au "
                    "réveil, avec rattrapage de la dernière fenêtre manquée "
                    "(autonomy/schedule.json : catch_up, catch_up_hours).\n\n"
                    "Le verrou anti-double exécution reste en place : une "
                    "fenêtre déjà honorée n'est jamais relancée."
                ),
                justify="left",
                wraplength=520,
                anchor="w",
            ).pack(fill="x", padx=16, pady=(16, 10))
            buttons = ctk.CTkFrame(window)
            buttons.pack(fill="x", padx=16, pady=(0, 12))
            ctk.CTkButton(
                buttons,
                text="Enregistrer la tâche",
                command=self._install_schedule_task,
            ).pack(side="left", padx=(0, 8))
            ctk.CTkButton(
                buttons,
                text="Désinscrire",
                fg_color="#B91C1C",
                hover_color="#991B1B",
                command=self._uninstall_schedule_task,
            ).pack(side="left", padx=(0, 8))
            ctk.CTkButton(buttons, text="Fermer", command=window.destroy).pack(
                side="left"
            )

        def _install_schedule_task(self) -> None:
            """Enregistre la tâche ; l'échec est dit, jamais silencieux."""
            from core.taskscheduler import install_task

            try:
                ok, message = install_task(self.settings)
            except Exception as exc:  # noqa: BLE001 - un refus ne casse pas l'UI
                ok, message = False, f"{type(exc).__name__}: {exc}"
            self._append("système" if ok else "erreur", message)
            self._set_status(
                "tâche planifiée" if ok else "enregistrement de la tâche refusé"
            )

        def _uninstall_schedule_task(self) -> None:
            """Désinscrit la tâche existante, sur demande explicite."""
            from core.taskscheduler import uninstall_task

            try:
                ok, message = uninstall_task()
            except Exception as exc:  # noqa: BLE001 - un refus ne casse pas l'UI
                ok, message = False, f"{type(exc).__name__}: {exc}"
            self._append("système" if ok else "erreur", message)
            self._set_status(
                "tâche désinscrite" if ok else "désinscription de la tâche refusée"
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

        def _open_journal(self) -> None:
            """Relit les derniers cycles autonomes : résultat et extrait."""
            from core.autonomy import AutonomyJournal, journal_path

            path = journal_path(self.settings)
            records = AutonomyJournal(
                path,
                max_entries=self.settings.autonomy.journal_max_entries,
                max_bytes=self.settings.autonomy.journal_max_bytes,
            ).tail(50)
            window = ctk.CTkToplevel(self)
            window.title("Journal d'autonomie")
            window.geometry("780x520")
            window.transient(self)
            frame = ctk.CTkScrollableFrame(window, label_text=str(path))
            frame.pack(fill="both", expand=True, padx=10, pady=10)
            ctk.CTkLabel(
                frame,
                text=format_autonomy_journal(records),
                justify="left",
                anchor="w",
                wraplength=700,
                font=(self.settings.ui.font_family, self.settings.ui.font_size - 1),
            ).pack(fill="x", padx=6, pady=6)

        def _open_proposals(self) -> None:
            """Propositions autonomes en attente : approuver ou rejeter, sans exécuter."""
            from core.proposals import (
                approve_proposal,
                pending_proposals,
                reject_proposal,
                set_proposal_task,
            )

            window = ctk.CTkToplevel(self)
            window.title("Propositions autonomes")
            window.geometry("780x460")
            window.transient(self)
            frame = ctk.CTkScrollableFrame(window, label_text="En attente d'approbation")
            frame.pack(fill="both", expand=True, padx=10, pady=10)
            pending = pending_proposals(self.settings)
            if not pending:
                ctk.CTkLabel(
                    frame, text="Aucune proposition en attente (mode --autonomy-propose)."
                ).pack(pady=20)
                return
            for record in pending:
                row = ctk.CTkFrame(frame)
                row.pack(fill="x", pady=4)
                detail = f"[{record.get('risk', 'inconnu')}] {record.get('task', '')}"
                if record.get("tools"):
                    detail += f"\n    outils : {', '.join(record['tools'])}"
                ctk.CTkLabel(
                    row, text=detail, anchor="w", justify="left", wraplength=540
                ).pack(side="left", padx=8, fill="x", expand=True)

                def approve(proposal_id: str = str(record.get("id"))) -> None:
                    approved = approve_proposal(self.settings, proposal_id)
                    window.destroy()
                    if approved is None:
                        return
                    self.prompt.delete("1.0", "end")
                    self.prompt.insert("1.0", str(approved.get("task") or ""))
                    self._approved_proposal = proposal_id
                    if not self._send():
                        # Rien n'a été lancé : ne pas laisser la proposition
                        # approuvée sans exécution ni clôture.
                        self._approved_proposal = None

                def reject(proposal_id: str = str(record.get("id"))) -> None:
                    reject_proposal(self.settings, proposal_id)
                    window.destroy()
                    self._open_proposals()

                def edit(proposal_id: str = str(record.get("id"))) -> None:
                    dialog = ctk.CTkInputDialog(
                        text="Nouvelle tache :", title="Modifier la proposition"
                    )
                    value = dialog.get_input()
                    if value and set_proposal_task(self.settings, proposal_id, value):
                        window.destroy()
                        self._open_proposals()

                ctk.CTkButton(row, text="Modifier", width=70, command=edit).pack(
                    side="right", padx=4, pady=4
                )
                ctk.CTkButton(row, text="Rejeter", width=70, command=reject).pack(
                    side="right", padx=4, pady=4
                )
                ctk.CTkButton(row, text="Approuver", width=80, command=approve).pack(
                    side="right", padx=4, pady=4
                )

        def _support_bundle(self) -> None:
            """Écrit un paquet de diagnostic rouge dans reports/."""
            from core.support_bundle import build_support_bundle

            try:
                path = build_support_bundle(self.settings)
            except OSError as exc:
                self._append("erreur", f"paquet de support impossible : {exc}")
                return
            self._append("système", f"paquet de support écrit : {path}")

        def _open_traces(self) -> None:
            """Liste les exécutions enregistrées et en relit une à la demande."""
            directory = self.settings.paths.log_dir / "runs"
            window = ctk.CTkToplevel(self)
            window.title("Traces d'exécution")
            window.geometry("760x480")
            window.transient(self)
            toolbar = ctk.CTkFrame(window)
            toolbar.pack(fill="x", padx=10, pady=(10, 0))
            report_label = ctk.CTkLabel(
                toolbar, text="", anchor="w", justify="left", wraplength=700
            )

            def run_maintenance(*, rebuild: bool) -> None:
                """Compacte ou reconstruit l'index sans bloquer sur un dossier absent."""
                from core.run_index import maintain

                if not directory.is_dir():
                    report_label.configure(text="Aucun dossier de traces : rien à entretenir.")
                    return
                try:
                    report = maintain(directory, rebuild=rebuild)
                except OSError as exc:
                    report_label.configure(text=f"maintenance impossible : {exc}")
                    return
                report_label.configure(text=format_maintenance_report(report))

            def run_doctor() -> None:
                """Explique les écarts index/traces, sans rien modifier."""
                from core.index_doctor import diagnose
                from core.index_doctor import format_issues as format_diagnosis

                if not directory.is_dir():
                    report_label.configure(
                        text="Aucun dossier de traces : rien à diagnostiquer."
                    )
                    return
                try:
                    issues = diagnose(directory)
                except OSError as exc:
                    report_label.configure(text=f"diagnostic impossible : {exc}")
                    return
                report_label.configure(text=format_diagnosis(issues, with_help=True)[:1800])

            for label_text, rebuild in (("Compacter", False), ("Reconstruire", True)):
                ctk.CTkButton(
                    toolbar,
                    text=label_text,
                    width=110,
                    command=lambda r=rebuild: run_maintenance(rebuild=r),
                ).pack(side="left", padx=(0, 6), pady=4)
            ctk.CTkButton(
                toolbar, text="Diagnostic", width=110, command=run_doctor
            ).pack(side="left", padx=(0, 6), pady=4)

            def show_metrics() -> None:
                from core.metrics import MetricsRecorder, format_summary, metrics_path

                records = MetricsRecorder(metrics_path(self.settings)).all()
                report_label.configure(text=format_summary(records))

            ctk.CTkButton(
                toolbar, text="Métriques", width=100, command=show_metrics
            ).pack(side="left", padx=(0, 6), pady=4)
            report_label.pack(fill="x", pady=(2, 4))
            filters = ctk.CTkFrame(window)
            filters.pack(fill="x", padx=10, pady=(2, 0))
            state_var = ctk.StringVar(value="tous")
            ctk.CTkLabel(filters, text="État").pack(side="left")
            ctk.CTkOptionMenu(
                filters,
                values=["tous", "finished", "started", "interrupted", "archived"],
                variable=state_var,
                width=120,
                command=lambda _value: refresh(),
            ).pack(side="left", padx=(4, 10))
            model_var = ctk.StringVar()
            workspace_var = ctk.StringVar()
            days_var = ctk.StringVar()
            for label, variable, width in (
                ("Modèle", model_var, 110),
                ("Atelier", workspace_var, 100),
                ("Depuis (j)", days_var, 70),
            ):
                ctk.CTkLabel(filters, text=label).pack(side="left", padx=(0, 3))
                entry = ctk.CTkEntry(filters, textvariable=variable, width=width)
                entry.pack(side="left", padx=(0, 8))
                entry.bind("<KeyRelease>", lambda _event: refresh())
            frame = ctk.CTkScrollableFrame(window, label_text=str(directory))
            frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))
            # L'index est un cache : absent ou illisible, la liste reste possible.
            entries: dict[str, Any] = {}
            if directory.is_dir():
                try:
                    from core.run_index import merged_entries

                    # Espaces de travail (`trace.per_workspace`) compris : un
                    # index par sous-dossier, fusionné pour la liste et les filtres.
                    entries = merged_entries(directory)
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
            selected: dict[str, Any] = {}

            def trace_key() -> tuple[Any, str]:
                trace_cfg = self.controller.agent.settings.trace
                if not trace_cfg.encrypt:
                    return None, ""
                from core.trace_crypto import TraceKeyError, load_key

                try:
                    return load_key(trace_cfg.key_env), ""
                except TraceKeyError as exc:
                    return None, str(exc)

            def compare() -> None:
                """Compare deux runs sélectionnés, comme `--diff`."""
                from core.replay import manifest_diff, replay

                if len(selected) != 2:
                    report_label.configure(text="Sélectionnez exactement deux runs.")
                    return
                key, error = trace_key()
                if error:
                    report_label.configure(text=f"trace chiffrée : {error}")
                    return
                paths = list(selected.values())
                try:
                    left, right = replay(paths[0], key=key), replay(paths[1], key=key)
                except Exception as exc:  # noqa: BLE001 - l'interface rapporte
                    report_label.configure(text=f"comparaison impossible : {exc}")
                    return
                differences = manifest_diff(left, right)
                report_label.configure(
                    text=(
                        f"{paths[0].name} vs {paths[1].name}\n"
                        + ("\n".join(differences) or "aucune différence de manifeste ou de bilan")
                    )[:1200]
                )

            def refresh() -> None:
                for child in frame.winfo_children():
                    child.destroy()
                try:
                    max_days = float(days_var.get().strip() or 0)
                except ValueError:
                    max_days = 0.0
                shown = 0
                for path in files:
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    stem = path.name[:-3] if path.name.endswith(".gz") else path.name
                    stem = stem[:-6] if stem.endswith(".jsonl") else stem
                    entry = entries.get(stem)
                    state = entry.state if entry is not None else ""
                    if state_var.get() != "tous" and state != state_var.get():
                        continue
                    if model_var.get().strip() and model_var.get().strip().lower() not in (
                        entry.model if entry is not None else ""
                    ).lower():
                        continue
                    if workspace_var.get().strip() and workspace_var.get().strip().lower() not in (
                        entry.workspace if entry is not None else ""
                    ).lower():
                        continue
                    if max_days and time.time() - stat.st_mtime > max_days * 86_400:
                        continue
                    shown += 1
                    row = ctk.CTkFrame(frame)
                    row.pack(fill="x", pady=2)
                    stamp = time.strftime("%d/%m %H:%M", time.localtime(stat.st_mtime))
                    label = f"{stem}   {stamp}   {max(1, stat.st_size // 1024)} Kio"
                    if state:
                        label += f"   [{state}]"
                    if entry is not None and entry.model:
                        label += f"   {entry.model}"
                    if entry is not None and entry.workspace:
                        label += f"   @{entry.workspace}"
                    variable = ctk.BooleanVar(value=stem in selected)

                    def toggle(checked: bool, run_id: str = stem, path: Any = path) -> None:
                        if checked:
                            selected[run_id] = path
                        else:
                            selected.pop(run_id, None)

                    ctk.CTkCheckBox(
                        row,
                        text="",
                        width=24,
                        variable=variable,
                        command=lambda v=variable, t=toggle: t(bool(v.get())),
                    ).pack(side="left", padx=(4, 0), pady=4)
                    ctk.CTkLabel(row, text=label, anchor="w").pack(
                        side="left", padx=8, fill="x", expand=True
                    )
                    ctk.CTkButton(
                        row, text="Lire", width=70, command=lambda p=path: self._show_trace(p)
                    ).pack(side="right", padx=6, pady=4)
                if not shown:
                    text = "Aucune trace enregistrée." if not files else "Aucun run ne correspond au filtre."
                    ctk.CTkLabel(frame, text=text).pack(pady=20)

            ctk.CTkButton(filters, text="Comparer", width=90, command=compare).pack(
                side="right"
            )
            refresh()

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

            hotkey_messages = getattr(self, "_hotkey_messages", None)
            while hotkey_messages is not None:
                try:
                    signal = hotkey_messages.get_nowait()
                except queue.Empty:
                    break
                if signal == "stop":
                    self._emergency_stop()

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

                # Clôture d'une proposition approuvée depuis l'interface : le
                # run est terminé, on rattache son identifiant et son résultat.
                proposal_id = getattr(self, "_approved_proposal", None)
                if proposal_id:
                    from core.proposals import finish_proposal

                    run_id = str(getattr(self.controller.agent, "run_id", "") or "")
                    record = finish_proposal(
                        self.settings,
                        proposal_id,
                        ok=bool(reply.ok),
                        run_id=run_id,
                        detail=(
                            f"interface : {reply.stop_reason}"
                            if reply.ok
                            else f"interface : {reply.stop_reason} {reply.error}".strip()
                        ),
                    )
                    self._approved_proposal = None
                    if record is not None:
                        self._append(
                            "système",
                            f"proposition {proposal_id} : {record.get('state')}"
                            + (f" (run {run_id})" if run_id else ""),
                        )

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

            elif (
                getattr(self, "_approved_proposal", None)
                and not self.controller.busy
                and self.controller.current is not None
                and getattr(self.controller.current, "finished", False)
            ):
                # Exception avant toute réponse : la proposition a bien été
                # exécutée, mais elle a échoué.
                from core.proposals import finish_proposal

                proposal_id = self._approved_proposal
                self._approved_proposal = None
                record = finish_proposal(
                    self.settings,
                    proposal_id,
                    ok=False,
                    run_id=str(getattr(self.controller.agent, "run_id", "") or ""),
                    detail=f"interface : {self.controller.current.error}"[:300],
                )
                if record is not None:
                    self._append("système", f"proposition {proposal_id} : failed")

            interval = max(30, int(self.settings.ui.stream_refresh_ms))
            self._pump_id = self.after(interval, self._pump)

        def _render(self, event: Event) -> None:
            payload: dict[str, Any] = event.payload if isinstance(event.payload, dict) else {}
            if event.type == EventType.RUN_STARTED and event.run_id:
                self._last_run_id = event.run_id
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
                self._hotkey.stop()
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
