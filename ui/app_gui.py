"""Fenêtre CustomTkinter de l'agent.

Le module s'importe même sans customtkinter installé : `available()` renvoie
alors False et `run_gui()` explique quoi installer. Rien de la logique de
l'agent ne vit ici — la fenêtre ne fait qu'afficher les événements du
`AgentController` et transmettre les actions de l'utilisateur.
"""

from __future__ import annotations

import logging
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

        def _pump(self) -> None:
            for event in self.controller.drain():
                self._render(event)

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
            elif event.type == EventType.ERROR:
                self._append("erreur", str(payload.get("message", "erreur inconnue")))

        # -- fermeture ------------------------------------------------------

        def _on_close(self) -> None:
            try:
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
