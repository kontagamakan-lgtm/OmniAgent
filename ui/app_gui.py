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
from pathlib import Path
from typing import Any

from config import APP_ID, APP_NAME, APP_VERSION, Settings
from core.events import Event, EventType
from core.factory import AgentBundle
from core.self_evolver import format_evolution_journal, format_evolution_status
from ui.controller import AgentController
from ui.hotkey import HotkeyListener
from ui.key_panel import KeyTable
from ui.layout import (
    LayoutPlan,
    fit_dialog_geometry,
    plan_layout,
    sidebar_needs_scroll,
)

try:  # pragma: no cover - dépend de l'environnement
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None  # type: ignore[assignment]

__all__ = ["OmniAgentWindow", "available", "run_gui"]

logger = logging.getLogger(f"{APP_ID}.ui")

# Palette de la maquette : fonds presque noirs, surfaces vert-gris et accent
# menthe. Les widgets de la fenêtre principale la reprennent explicitement,
# pour que le thème reste cohérent même si CustomTkinter change ses défauts.
_UI_BG = "#101313"
_UI_SIDEBAR = "#131717"
_UI_SURFACE = "#191D1D"
_UI_SURFACE_2 = "#1E2322"
_UI_FIELD = "#111612"
_UI_LINE = "#2A302E"
_UI_TEXT = "#ECF0ED"
_UI_MUTED = "#8C9691"
_UI_ACCENT_DARK = "#263B29"
_UI_RED = "#ED8F89"

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

    def __init__(
        self, widget: Any, *, max_lines: int = 0, trim_chunk: int = 200
    ) -> None:
        self.widget = widget
        self.max_lines = max(0, int(max_lines))
        self.trim_chunk = max(1, int(trim_chunk))

    def write(self, text: str) -> None:
        if not text:
            return
        self.widget.configure(state="normal")
        self.widget.insert("end", text)
        self.widget.see("end")
        self.widget.configure(state="disabled")
        self._trim()

    def _trim(self) -> None:
        """Borne la transcription des très longues sessions.

        La coupe se fait par blocs, uniquement quand le plafond est dépassé,
        pour ne pas déplacer le curseur à chaque fragment. Les widgets de test
        sans `index` sont laissés intacts.
        """
        if self.max_lines <= 0 or not hasattr(self.widget, "index"):
            return
        try:
            total = int(str(self.widget.index("end-1c")).split(".", 1)[0])
        except (TypeError, ValueError, IndexError):
            return
        if total <= self.max_lines + self.trim_chunk:
            return
        cut = total - self.max_lines
        if cut <= 0:
            return
        self.widget.configure(state="normal")
        self.widget.delete("1.0", f"{cut + 1}.0")
        self.widget.insert(
            "1.0", "• historique ancien tronqué pour garder l'interface fluide\n\n"
        )
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
            theme = Path(__file__).resolve().parent.parent / "assets" / "theme.json"
            ctk.set_default_color_theme(str(theme) if theme.is_file() else "blue")
            super().__init__()
            icon = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
            if icon.is_file():
                try:
                    self.iconbitmap(str(icon))
                except Exception:  # noqa: BLE001 - icône non bloquante
                    logger.debug("icône de fenêtre non chargée", exc_info=True)

            self.settings = settings
            # L'accent vient de la configuration : `ui.accent` n'est plus une
            # valeur morte, tout en gardant le vert menthe par défaut.
            self._accent = settings.ui.accent
            self.bundle = bundle
            self.controller = AgentController(bundle.agent)
            self._pump_id: str | None = None
            self._step = 0
            #: Dernier run tracé : support du bouton « Annuler ce run ».
            self._last_run_id = ""
            self._budget_at = 0.0
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
            #: Mode compact pour la taille minimale 900×620 : les cartes de
            #: synthèse et le pied de page s'effacent, l'accueil se réduit à
            #: son titre et la barre latérale se densifie.
            self._compact_layout = False
            self._layout_ready = False
            #: Besoins mesurés une fois en mode normal : largeur totale,
            #: hauteur du contenu et hauteur de la sidebar. Ils remplacent les
            #: seuils fixes 1100×720 et suivent la police ou les libellés.
            self._normal_requirements: tuple[float, float, float] | None = None
            self._sidebar_scroll_visible = False
            self._original_fonts: dict[Any, tuple] = {}

            self.title(f"{APP_NAME} {APP_VERSION}")
            self.geometry(f"{settings.ui.window_width}x{settings.ui.window_height}")
            self.minsize(900, 620)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

            self._build_widgets()
            # Verre dépoli : DWM floute lui-même ce qui est derrière la fenêtre.
            # Sans effet hors Windows, et l'interface reste opaque si DWM refuse.
            try:
                from ui.glass import apply_glass_backdrop

                apply_glass_backdrop(self)
            except Exception:  # noqa: BLE001 - un effet visuel n'est pas bloquant
                logger.debug("verre dépoli non appliqué", exc_info=True)
            self.bind("<Configure>", self._on_window_configure, add="+")
            self.bind("<F6>", self._focus_navigation)
            self.bind("<Shift-F6>", lambda _event: self._focus_composer())
            self.after_idle(self._apply_layout_mode)
            try:
                self._install_approval_gate()
            except Exception:  # noqa: BLE001 - l'interface reste utilisable sans porte
                logger.debug("porte d'approbation non installée", exc_info=True)
            evolver = getattr(self.bundle, "evolver", None)
            if evolver is not None:
                try:
                    if evolver.start():
                        self._append("système", "auto-évolution active en arrière-plan")
                except Exception:  # noqa: BLE001 - jamais bloquant
                    logger.debug("démarrage de l'auto-évolution impossible", exc_info=True)
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

        # -- style ---------------------------------------------------------

        def _font(self, delta: int = 0, weight: str = "normal") -> tuple:
            """Police de l'interface, dimensionnée depuis les réglages."""
            return (
                self.settings.ui.font_family,
                max(9, int(self.settings.ui.font_size) + delta),
                weight,
            )

        def _window_scale(self) -> float:
            """Facteur DPI de la fenêtre, avec repli sûr avant le mapping."""
            try:
                return max(1.0, float(ctk.ScalingTracker.get_window_scaling(self)))
            except Exception:  # noqa: BLE001 - fenêtre en cours de destruction
                return 1.0

        def _fit_dialog(self, window: Any, width: int, height: int) -> tuple[int, int]:
            """Centre une fenêtre secondaire sans la laisser déborder."""
            actual_w, actual_h, x, y = fit_dialog_geometry(
                parent_width=self.winfo_width(),
                parent_height=self.winfo_height(),
                screen_width=self.winfo_screenwidth(),
                screen_height=self.winfo_screenheight(),
                desired_width=width,
                desired_height=height,
            )
            window.geometry(
                f"{actual_w}x{actual_h}+{self.winfo_rootx() + x}+{self.winfo_rooty() + y}"
            )
            return actual_w, actual_h

        def _apply_compact_typography(self, compact: bool) -> None:
            """Réduit les polices en compact si la configuration est extrême.

            Sans ce garde-fou, ``ui.font_size=28`` rend la sidebar et les
            en-têtes plus larges que la fenêtre minimale. Les tailles d'origine
            sont restaurées au retour en mode normal.
            """
            font_size = int(self.settings.ui.font_size)
            if not compact or font_size <= 18:
                for widget, original in list(self._original_fonts.items()):
                    try:
                        if widget.winfo_exists():
                            widget.configure(font=original)
                    except Exception:  # noqa: BLE001 - widget détruit
                        pass
                self._original_fonts.clear()
                return
            factor = 14.0 / font_size

            def visit(widget: Any) -> None:
                try:
                    current = widget.cget("font")
                except Exception:  # noqa: BLE001 - widget sans police
                    current = None
                if isinstance(current, tuple) and len(current) >= 2:
                    original = self._original_fonts.setdefault(widget, current)
                    size = int(original[1])
                    scaled = max(9, int(round(size * factor)))
                    if scaled != size:
                        try:
                            widget.configure(
                                font=(original[0], scaled, *original[2:])
                            )
                        except Exception:  # noqa: BLE001 - police non modifiable
                            pass
                for child in widget.winfo_children():
                    visit(child)

            visit(self)

        def _button(
            self,
            parent: Any,
            text: str,
            command: Any,
            *,
            primary: bool = False,
            danger: bool = False,
            ghost: bool = False,
            width: int = 120,
            height: int = 34,
            anchor: str = "center",
            font_delta: int = -1,
        ) -> Any:
            """Bouton cohérent avec la maquette, sans perdre les actions existantes."""
            options: dict[str, Any] = {
                "text": text,
                "command": command,
                "width": width,
                "height": height,
                "anchor": anchor,
                "corner_radius": 7,
                "font": self._font(font_delta, "bold" if primary else "normal"),
            }
            if primary:
                options.update(
                    fg_color=self._accent,
                    hover_color="#C9F7C5",
                    text_color="#1C301E",
                    border_width=0,
                )
            elif danger:
                options.update(
                    fg_color="#2A1D1B",
                    hover_color="#3A2522",
                    text_color=_UI_RED,
                    border_width=1,
                    border_color="#4A302E",
                )
            elif ghost:
                options.update(
                    fg_color="transparent",
                    hover_color=_UI_SURFACE_2,
                    text_color=_UI_MUTED,
                    border_width=0,
                )
            else:
                options.update(
                    fg_color=_UI_SURFACE_2,
                    hover_color="#273027",
                    text_color=_UI_TEXT,
                    border_width=1,
                    border_color=_UI_LINE,
                )
            return ctk.CTkButton(parent, **options)

        def _nav(
            self,
            parent: Any,
            text: str,
            command: Any,
            *,
            active: bool = False,
            glyph: str = "·",
        ) -> Any:
            """Entrée de la barre latérale, avec l'état actif de la maquette."""
            button = self._button(
                parent,
                f"{glyph}   {text}",
                command,
                ghost=True,
                width=190,
                height=38,
                anchor="w",
                font_delta=-1,
            )
            if active:
                button.configure(
                    fg_color=_UI_ACCENT_DARK,
                    hover_color="#2E4032",
                    text_color=self._accent,
                )
            button.pack(fill="x", pady=2)
            self._nav_buttons.append(button)
            return button

        def _card(self, parent: Any) -> Any:
            return ctk.CTkFrame(
                parent,
                fg_color=_UI_SURFACE,
                border_width=1,
                border_color=_UI_LINE,
                corner_radius=10,
            )

        def _panel_title(self, parent: Any, title: str) -> Any:
            header = ctk.CTkFrame(parent, fg_color="transparent", height=44)
            header.pack(fill="x", padx=16)
            header.pack_propagate(False)
            ctk.CTkLabel(
                header,
                text=title,
                font=self._font(-2, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
            ).pack(side="left", fill="y")
            return header

        def _focus_composer(self) -> None:
            """Cliquer sur « Assistant » ramène au composeur sans effacer la session."""
            self.prompt.focus_set()

        def _focus_navigation(self, _event: object | None = None) -> str:
            """F6 place le focus sur la première entrée de navigation visible."""
            for button in self._nav_buttons:
                if button.winfo_viewable():
                    button.focus_set()
                    break
            return "break"

        def _new_session(self) -> None:
            """Vide la conversation affichée sans toucher aux traces sur disque."""
            if getattr(self.controller, "busy", False):
                self._set_status("une tâche est déjà en cours")
                return
            thread = vars(self).get("_autonomy_thread")
            if thread is not None and thread.is_alive():
                self._set_status("session autonome en cours : arrêtez-la d'abord")
                return
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.configure(state="disabled")
            self.transcript.grid_remove()
            welcome = vars(self).get("welcome_frame")
            if welcome is not None:
                welcome.grid()
            self._tasks_done = 0
            count = vars(self).get("task_count_label")
            if count is not None:
                count.configure(text="0")
            self.prompt.delete("1.0", "end")
            self._set_status("prêt")
            self.prompt.focus_set()

        def _build_widgets(self) -> None:
            self.configure(fg_color=_UI_BG)
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self._tasks_done = 0

            # -- barre latérale ------------------------------------------------
            # Défilement vertical disponible seulement si le contenu dépasse :
            # la scrollbar reste cachée en 1280×860 et lorsque la densité
            # compacte suffit à 900×620.
            side = ctk.CTkScrollableFrame(
                self,
                width=226,
                corner_radius=0,
                fg_color=_UI_SIDEBAR,
                scrollbar_button_color=_UI_LINE,
                scrollbar_button_hover_color="#3A4739",
            )
            side.grid(row=0, column=0, sticky="ns")
            self.sidebar = side
            self._sidebar_scrollbar = getattr(side, "_scrollbar", None)
            if self._sidebar_scrollbar is not None:
                self._sidebar_scrollbar.grid_remove()

            brand = ctk.CTkFrame(side, fg_color="transparent")
            brand.pack(fill="x", padx=16, pady=(28, 0))
            self.side_brand = brand
            ctk.CTkLabel(
                brand,
                text="◆",
                width=34,
                font=self._font(8, "bold"),
                text_color=self._accent,
            ).pack(side="left")
            ctk.CTkLabel(
                brand, text="Omni", font=self._font(8, "bold"), text_color=_UI_TEXT
            ).pack(side="left")
            ctk.CTkLabel(
                brand,
                text="Agent",
                font=self._font(8, "bold"),
                text_color=self._accent,
            ).pack(side="left")

            workspace_switch = ctk.CTkFrame(
                side,
                fg_color=_UI_SURFACE,
                border_width=1,
                border_color=_UI_LINE,
                corner_radius=8,
            )
            workspace_switch.pack(fill="x", padx=16, pady=(28, 22))
            self.workspace_switch = workspace_switch
            ctk.CTkLabel(
                workspace_switch,
                text="▦",
                width=28,
                font=self._font(2),
                text_color=self._accent,
            ).pack(side="left", padx=(10, 8), pady=10)
            workspace_text = ctk.CTkFrame(workspace_switch, fg_color="transparent")
            workspace_text.pack(side="left", pady=9)
            ctk.CTkLabel(
                workspace_text,
                text="Espace personnel",
                font=self._font(-1, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
            ).pack(fill="x")
            ctk.CTkLabel(
                workspace_text,
                text="Windows · Local",
                font=self._font(-3),
                text_color=_UI_MUTED,
                anchor="w",
            ).pack(fill="x")

            self.workspace_label = ctk.CTkLabel(
                side,
                text="ESPACE DE TRAVAIL",
                font=self._font(-3, "bold"),
                text_color="#68746D",
                anchor="w",
            )
            self.workspace_label.pack(fill="x", padx=24, pady=(0, 8))
            self._nav_buttons: list[Any] = []
            primary_nav = ctk.CTkFrame(side, fg_color="transparent")
            primary_nav.pack(fill="x", padx=12)
            self._nav(
                primary_nav,
                "Assistant",
                self._focus_composer,
                active=True,
                glyph="◆",
            )
            self._nav(
                primary_nav, "Historique", self._open_journal, glyph="◷"
            )
            self._nav(
                primary_nav, "Planificateur", self._open_scheduler, glyph="↻"
            )
            self._nav(
                primary_nav, "Propositions", self._open_proposals, glyph="✦"
            )

            self.config_label = ctk.CTkLabel(
                side,
                text="CONFIGURATION",
                font=self._font(-3, "bold"),
                text_color="#68746D",
                anchor="w",
            )
            self.config_label.pack(fill="x", padx=24, pady=(24, 8))
            config_nav = ctk.CTkFrame(side, fg_color="transparent")
            config_nav.pack(fill="x", padx=12)
            self._nav(
                config_nav, "Autonomie", self._toggle_autonomy, glyph="⌁"
            )
            self._nav(
                config_nav, "Évolution", self._open_evolution, glyph="⚡"
            )
            self._nav(
                config_nav, "Clés & modèles", self._open_keys, glyph="⌘"
            )
            self._nav(
                config_nav,
                "Directives",
                self._open_prime_directive,
                glyph="▤",
            )
            self._nav(
                config_nav,
                "Configuration initiale",
                self._open_assistant,
                glyph="⚙",
            )

            sidebar_bottom = ctk.CTkFrame(side, fg_color="transparent")
            sidebar_bottom.pack(side="bottom", fill="x", padx=16, pady=16)
            self.sidebar_bottom = sidebar_bottom
            ctk.CTkFrame(
                sidebar_bottom, height=1, fg_color=_UI_LINE
            ).pack(fill="x", pady=(0, 12))
            profile = ctk.CTkFrame(sidebar_bottom, fg_color="transparent")
            profile.pack(fill="x")
            ctk.CTkLabel(
                profile,
                text="OA",
                width=32,
                height=32,
                corner_radius=16,
                fg_color="#38382F",
                text_color="#D4D5B6",
                font=self._font(-2, "bold"),
            ).pack(side="left")
            profile_text = ctk.CTkFrame(profile, fg_color="transparent")
            profile_text.pack(side="left", padx=9)
            ctk.CTkLabel(
                profile_text,
                text="OmniAgent",
                font=self._font(-2),
                text_color=_UI_TEXT,
                anchor="w",
            ).pack(fill="x")
            ctk.CTkLabel(
                profile_text,
                text="Session locale",
                font=self._font(-3),
                text_color=_UI_MUTED,
                anchor="w",
            ).pack(fill="x")

            # -- zone principale ----------------------------------------------
            main = ctk.CTkFrame(self, fg_color=_UI_BG, corner_radius=0)
            main.grid(row=0, column=1, sticky="nsew")
            main.grid_columnconfigure(0, weight=1)
            main.grid_rowconfigure(1, weight=1)

            topbar = ctk.CTkFrame(
                main, height=75, fg_color=_UI_BG, corner_radius=0
            )
            topbar.grid(row=0, column=0, sticky="ew", padx=34)
            topbar.grid_propagate(False)
            topbar.grid_columnconfigure(1, weight=1)

            crumbs = ctk.CTkFrame(topbar, fg_color="transparent")
            crumbs.grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(
                crumbs,
                text="▦  Espace de travail",
                font=self._font(-2),
                text_color="#89958D",
            ).pack(side="left")
            ctk.CTkLabel(
                crumbs,
                text="/",
                font=self._font(-2),
                text_color="#48514B",
            ).pack(side="left", padx=9)
            ctk.CTkLabel(
                crumbs,
                text="Assistant",
                font=self._font(-1),
                text_color="#D6DDD7",
            ).pack(side="left")

            top_actions = ctk.CTkFrame(topbar, fg_color="transparent")
            top_actions.grid(row=0, column=2, sticky="e")
            version = ctk.CTkFrame(
                top_actions,
                fg_color="transparent",
                border_width=1,
                border_color=_UI_LINE,
                corner_radius=4,
            )
            version.pack(side="left", padx=(0, 10))
            ctk.CTkLabel(
                version,
                text=f"v{APP_VERSION}",
                font=self._font(-3),
                text_color=_UI_MUTED,
            ).pack(padx=7, pady=3)
            self._button(
                top_actions,
                "?",
                self._support_bundle,
                ghost=True,
                width=30,
                height=30,
                font_delta=1,
            ).pack(side="left")
            self.layout_badge = ctk.CTkLabel(
                top_actions,
                text="Mode compact",
                font=self._font(-4),
                text_color="#B4C5A8",
                fg_color="#243027",
                corner_radius=4,
                padx=6,
                pady=3,
            )
            # Affiché uniquement en compact par `_apply_layout_mode`.

            content = ctk.CTkFrame(main, fg_color=_UI_BG, corner_radius=0)
            content.grid(
                row=1, column=0, sticky="nsew", padx=34, pady=(12, 20)
            )
            self.content_panel = content
            content.grid_columnconfigure(0, weight=1)
            content.grid_rowconfigure(2, weight=1)

            heading = ctk.CTkFrame(content, fg_color="transparent")
            heading.grid(row=0, column=0, sticky="ew", pady=(6, 22))
            self.heading = heading
            heading.grid_columnconfigure(0, weight=1)
            heading_text = ctk.CTkFrame(heading, fg_color="transparent")
            heading_text.grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(
                heading_text,
                text="VOTRE COPILOTE AU QUOTIDIEN",
                font=self._font(-3, "bold"),
                text_color=self._accent,
                anchor="w",
            ).pack(fill="x", pady=(0, 8))
            self.heading_title = ctk.CTkLabel(
                heading_text,
                text="À vous de donner le cap.",
                font=self._font(9, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
            )
            self.heading_title.pack(fill="x")
            self.heading_subtitle = ctk.CTkLabel(
                heading_text,
                text="Une tâche en tête ? Votre agent s'occupe de la suite.",
                font=self._font(-1),
                text_color=_UI_MUTED,
                anchor="w",
            )
            self.heading_subtitle.pack(fill="x", pady=(6, 0))
            heading_actions = ctk.CTkFrame(heading, fg_color="transparent")
            heading_actions.grid(row=0, column=1, sticky="e")
            self.heading_actions = heading_actions
            self._button(
                heading_actions,
                "Voir l'historique",
                self._open_journal,
                ghost=True,
                width=130,
            ).pack(side="left", padx=(0, 8))
            self._button(
                heading_actions,
                "+  Nouvelle session",
                self._new_session,
                primary=True,
                width=145,
            ).pack(side="left")

            summary = ctk.CTkFrame(content, fg_color="transparent")
            summary.grid(row=1, column=0, sticky="ew", pady=(0, 20))
            self.summary_panel = summary
            for column in range(3):
                summary.grid_columnconfigure(column, weight=1, uniform="stat")

            stat_agent = self._card(summary)
            stat_agent.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
            stat_agent.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                stat_agent,
                text="◆",
                width=38,
                height=38,
                corner_radius=8,
                fg_color="#232B24",
                text_color="#A6BBA5",
                font=self._font(2, "bold"),
            ).grid(row=0, column=0, rowspan=2, padx=(14, 11), pady=14)
            ctk.CTkLabel(
                stat_agent,
                text="Statut de l'agent",
                font=self._font(-3),
                text_color="#96A197",
                anchor="w",
            ).grid(row=0, column=1, sticky="sw", pady=(14, 0))
            self.status = ctk.CTkLabel(
                stat_agent,
                text="Prêt à travailler",
                font=self._font(1, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
                justify="left",
                wraplength=240,
            )
            self.status.grid(row=1, column=1, sticky="nw", pady=(2, 14))

            stat_tasks = self._card(summary)
            stat_tasks.grid(row=0, column=1, sticky="nsew", padx=7)
            stat_tasks.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                stat_tasks,
                text="✓",
                width=38,
                height=38,
                corner_radius=8,
                fg_color="#232B24",
                text_color="#A6BBA5",
                font=self._font(4, "bold"),
            ).grid(row=0, column=0, rowspan=2, padx=(14, 11), pady=14)
            ctk.CTkLabel(
                stat_tasks,
                text="Tâches effectuées",
                font=self._font(-3),
                text_color="#96A197",
                anchor="w",
            ).grid(row=0, column=1, sticky="sw", pady=(14, 0))
            self.task_count_label = ctk.CTkLabel(
                stat_tasks,
                text="0",
                font=self._font(1, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
            )
            self.task_count_label.grid(row=1, column=1, sticky="nw", pady=(2, 14))

            stat_budget = self._card(summary)
            stat_budget.grid(row=0, column=2, sticky="nsew", padx=(7, 0))
            stat_budget.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                stat_budget,
                text="$",
                width=38,
                height=38,
                corner_radius=8,
                fg_color="#232B24",
                text_color="#A6BBA5",
                font=self._font(3, "bold"),
            ).grid(row=0, column=0, rowspan=2, padx=(14, 11), pady=14)
            ctk.CTkLabel(
                stat_budget,
                text="Budget utilisé aujourd'hui",
                font=self._font(-3),
                text_color="#96A197",
                anchor="w",
            ).grid(row=0, column=1, sticky="sw", pady=(14, 0))
            self.budget_card_label = ctk.CTkLabel(
                stat_budget,
                text="0,00 $",
                font=self._font(1, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
            )
            self.budget_card_label.grid(row=1, column=1, sticky="nw", pady=(2, 14))
            ctk.CTkLabel(
                stat_budget,
                text="USD",
                font=self._font(-3, "bold"),
                text_color="#B4D6AB",
                fg_color="#28392B",
                corner_radius=4,
                width=30,
            ).grid(row=1, column=2, sticky="e", padx=(0, 12))

            workspace = ctk.CTkFrame(content, fg_color="transparent")
            workspace.grid(row=2, column=0, sticky="nsew")
            workspace.grid_columnconfigure(0, weight=1)
            workspace.grid_columnconfigure(1, minsize=290)
            workspace.grid_rowconfigure(0, weight=1)

            assistant = self._card(workspace)
            assistant.grid(row=0, column=0, sticky="nsew")
            self.assistant_panel = assistant
            assistant.grid_columnconfigure(0, weight=1)
            assistant.grid_rowconfigure(1, weight=1)

            panel_header = ctk.CTkFrame(
                assistant, fg_color="transparent", height=60
            )
            panel_header.grid(row=0, column=0, sticky="ew")
            panel_header.grid_propagate(False)
            panel_header.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                panel_header,
                text="✦  Votre assistant",
                font=self._font(-2, "bold"),
                text_color=_UI_TEXT,
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=20, pady=20)
            self.session_status = ctk.CTkLabel(
                panel_header,
                text="● En ligne",
                font=self._font(-3, "bold"),
                text_color="#ABC6A6",
                fg_color="#243027",
                corner_radius=20,
                padx=9,
                pady=4,
                justify="right",
                wraplength=220,
            )
            self.session_status.grid(row=0, column=1, sticky="e", padx=18)

            conversation = ctk.CTkFrame(assistant, fg_color=_UI_SURFACE)
            conversation.grid(row=1, column=0, sticky="nsew")
            conversation.grid_columnconfigure(0, weight=1)
            conversation.grid_rowconfigure(0, weight=1)

            self.welcome_frame = ctk.CTkFrame(conversation, fg_color=_UI_SURFACE)
            self.welcome_frame.grid(row=0, column=0, sticky="nsew")
            welcome_center = ctk.CTkFrame(
                self.welcome_frame, fg_color="transparent"
            )
            welcome_center.pack(expand=True, pady=(22, 12))
            self.welcome_center = welcome_center
            self.welcome_icon = ctk.CTkLabel(
                welcome_center,
                text="◆",
                width=58,
                height=58,
                corner_radius=17,
                fg_color="#29382A",
                text_color=self._accent,
                font=self._font(12, "bold"),
            )
            self.welcome_icon.pack()
            self.welcome_title = ctk.CTkLabel(
                welcome_center,
                text="Moins de tâches. Plus de possibilités.",
                font=self._font(5, "bold"),
                text_color=_UI_TEXT,
            )
            self.welcome_title.pack(pady=(15, 5))
            self.welcome_subtitle = ctk.CTkLabel(
                welcome_center,
                text=(
                    "Décrivez ce que vous souhaitez accomplir.\n"
                    "Je planifie, j'exécute, vous gardez le contrôle."
                ),
                font=self._font(-1),
                text_color=_UI_MUTED,
                justify="center",
            )
            self.welcome_subtitle.pack(pady=(0, 17))

            self.transcript = ctk.CTkTextbox(
                conversation,
                wrap="word",
                state="disabled",
                fg_color=_UI_SURFACE,
                border_width=0,
                corner_radius=0,
                text_color=_UI_TEXT,
                font=self._font(-1),
                scrollbar_button_color="#3A4739",
                scrollbar_button_hover_color="#4B5A42",
            )
            self.transcript.grid(row=0, column=0, sticky="nsew", padx=18, pady=12)
            self.transcript.grid_remove()
            self._buffer = TranscriptBuffer(self.transcript, max_lines=6000)

            composer_wrap = ctk.CTkFrame(assistant, fg_color=_UI_SURFACE)
            composer_wrap.grid(row=2, column=0, sticky="ew", padx=17, pady=(2, 13))
            composer = ctk.CTkFrame(
                composer_wrap,
                fg_color=_UI_FIELD,
                border_width=1,
                border_color="#3A4739",
                corner_radius=9,
            )
            composer.pack(fill="x")
            self.prompt = ctk.CTkTextbox(
                composer,
                height=62,
                wrap="word",
                fg_color=_UI_FIELD,
                border_width=0,
                text_color=_UI_TEXT,
                font=self._font(-1),
            )
            self.prompt.pack(fill="x", padx=11, pady=(8, 0))
            self.prompt.bind("<Control-Return>", lambda _event: self._send())

            composer_bottom = ctk.CTkFrame(composer, fg_color="transparent")
            composer_bottom.pack(fill="x", padx=9, pady=(2, 8))
            composer_tools = ctk.CTkFrame(
                composer_bottom, fg_color="transparent"
            )
            composer_tools.pack(side="left")
            self._button(
                composer_tools,
                "Traces",
                self._open_traces,
                ghost=True,
                width=64,
                height=26,
                font_delta=-3,
            ).pack(side="left")
            self.undo_button = self._button(
                composer_tools,
                "Annuler ce run",
                self._undo_last_run,
                ghost=True,
                width=105,
                height=26,
                font_delta=-3,
            )
            self.undo_button.pack(side="left", padx=2)
            self._button(
                composer_tools,
                "Journal",
                self._open_journal,
                ghost=True,
                width=64,
                height=26,
                font_delta=-3,
            ).pack(side="left")
            self.send_button = self._button(
                composer_bottom,
                "Envoyer  →",
                self._send,
                primary=True,
                width=105,
                height=30,
                font_delta=-2,
            )
            self.send_button.pack(side="right")
            self.cancel_button = self._button(
                composer_bottom,
                "Annuler",
                self._cancel,
                ghost=True,
                width=66,
                height=30,
                font_delta=-2,
            )
            self.cancel_button.configure(state="disabled")
            self.cancel_button.pack(side="right", padx=(0, 4))

            composer_note = ctk.CTkFrame(assistant, fg_color=_UI_SURFACE)
            composer_note.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 12))
            self.composer_note_label = ctk.CTkLabel(
                composer_note,
                text="Ctrl + Entrée pour envoyer  ·  Entrée pour une nouvelle ligne",
                font=self._font(-4),
                text_color="#68736A",
                anchor="w",
                justify="left",
            )
            self.composer_note_label.pack(side="left")

            # -- colonne de droite ---------------------------------------------
            right = ctk.CTkFrame(workspace, fg_color=_UI_BG)
            right.grid(row=0, column=1, sticky="nsew", padx=(20, 0))
            right.grid_columnconfigure(0, weight=1)
            right.grid_rowconfigure(0, weight=1)

            session = self._card(right)
            session.grid(row=0, column=0, sticky="new")
            self._panel_title(session, "Contrôle de la session")
            session_body = ctk.CTkFrame(session, fg_color="transparent")
            session_body.pack(fill="x", padx=16, pady=(2, 10))

            mode_row = ctk.CTkFrame(session_body, fg_color="transparent")
            mode_row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                mode_row,
                text="Mode d'exécution",
                font=self._font(-3),
                text_color="#99A498",
            ).pack(side="left")
            self.mode_label = ctk.CTkLabel(
                mode_row,
                text="● Supervisé",
                font=self._font(-3, "bold"),
                text_color="#B4C5A8",
            )
            self.mode_label.pack(side="right")

            autonomy_row = ctk.CTkFrame(session_body, fg_color="transparent")
            autonomy_row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                autonomy_row,
                text="Session autonome",
                font=self._font(-3),
                text_color="#99A498",
            ).pack(side="left")
            self.autonomy_button = self._button(
                autonomy_row,
                "Lancer",
                self._toggle_autonomy,
                ghost=False,
                width=76,
                height=26,
                font_delta=-3,
            )
            self.autonomy_button.pack(side="right")

            trace_row = ctk.CTkFrame(session_body, fg_color="transparent")
            trace_row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                trace_row,
                text="Traces détaillées",
                font=self._font(-3),
                text_color="#99A498",
            ).pack(side="left")
            self.trace_switch = ctk.CTkSwitch(
                trace_row,
                text="",
                width=38,
                height=18,
                fg_color="#414A40",
                progress_color=self._accent,
                button_color="#182218",
                button_hover_color="#101810",
                command=self._toggle_trace,
            )
            self.trace_switch.pack(side="right")
            if self.controller.tracing:
                self.trace_switch.select()
            else:
                self.trace_switch.deselect()

            ctk.CTkFrame(session_body, height=1, fg_color=_UI_LINE).pack(
                fill="x", pady=(8, 6)
            )
            budget_row = ctk.CTkFrame(session_body, fg_color="transparent")
            budget_row.pack(fill="x")
            ctk.CTkLabel(
                budget_row,
                text="Budget quotidien",
                font=self._font(-3),
                text_color=_UI_MUTED,
            ).pack(side="left")
            self.budget_label = ctk.CTkLabel(
                budget_row,
                text="0,00 $ · sans plafond",
                font=self._font(-3, "bold"),
                text_color="#DBE3D5",
                anchor="e",
                justify="right",
                wraplength=180,
            )
            self.budget_label.pack(side="right")
            self.budget_progress = ctk.CTkProgressBar(
                session_body,
                height=4,
                corner_radius=4,
                fg_color="#2E362B",
                progress_color=self._accent,
            )
            self.budget_progress.pack(fill="x", pady=(10, 5))
            self.budget_progress.set(0)
            self.budget_reset_label = ctk.CTkLabel(
                session_body,
                text="Réinitialisation à minuit",
                font=self._font(-4),
                text_color="#71806F",
                anchor="w",
            )
            self.budget_reset_label.pack(fill="x")

            self.emergency_button = self._button(
                right,
                "Arrêt d'urgence   Ctrl + Alt + S",
                self._emergency_stop,
                danger=True,
                width=290,
                height=34,
                font_delta=-3,
            )
            self.emergency_button.grid(
                row=1, column=0, sticky="ew", pady=(12, 0)
            )

            footer = ctk.CTkFrame(content, fg_color="transparent")
            footer.grid(row=3, column=0, sticky="ew", pady=(16, 0))
            self.footer = footer
            ctk.CTkLabel(
                footer,
                text="◇  Vos données restent sur votre machine. Votre confiance, notre priorité.",
                font=self._font(-4),
                text_color="#5E6C60",
                anchor="w",
            ).pack(side="left")
            self.bind_all("<Control-Alt-s>", lambda _event: self._emergency_stop())

        def _on_window_configure(self, event: Any) -> None:
            """Bascule le mode compact sans traiter les Configure des enfants."""
            if event.widget is self:
                self._apply_layout_mode(event.width, event.height)
                self.after_idle(self._sync_sidebar_scroll)

        def _sidebar_content_height(self) -> float:
            """Hauteur réelle du contenu, même dans un CTkScrollableFrame."""
            canvas = getattr(self.sidebar, "_parent_canvas", None)
            if canvas is not None:
                try:
                    bbox = canvas.bbox("all")
                except Exception:  # noqa: BLE001 - fenêtre en destruction
                    bbox = None
                if bbox:
                    return float(bbox[3] - bbox[1])
            return float(self.sidebar.winfo_reqheight())

        def _sync_sidebar_scroll(self) -> None:
            """N'affiche la scrollbar que si le contenu ne tient pas."""
            scrollbar = getattr(self, "_sidebar_scrollbar", None)
            if scrollbar is None or not hasattr(self, "sidebar"):
                return
            try:
                canvas = getattr(self.sidebar, "_parent_canvas", None)
                viewport = max(
                    1,
                    canvas.winfo_height()
                    if canvas is not None
                    else self.sidebar.winfo_height(),
                )
                if viewport <= 10:
                    # Avant le premier layout, le canvas vaut 1 px : attendre
                    # une passe au lieu d'afficher une scrollbar inutile.
                    self.after(50, self._sync_sidebar_scroll)
                    return
                content = self._sidebar_content_height()
                needed = sidebar_needs_scroll(content, viewport)
                if needed == self._sidebar_scroll_visible:
                    return
                self._sidebar_scroll_visible = needed
                if needed:
                    scrollbar.grid()
                else:
                    scrollbar.grid_remove()
                    canvas = getattr(self.sidebar, "_parent_canvas", None)
                    if canvas is not None:
                        canvas.yview_moveto(0)
            except Exception:  # noqa: BLE001 - jamais bloquant
                logger.debug("synchronisation de la scrollbar impossible", exc_info=True)

        def _measure_normal_requirements(self) -> tuple[float, float, float]:
            """Mesure le mode normal : largeur totale, hauteur et sidebar."""
            self.update_idletasks()
            scale = self._window_scale()
            side_width = self.sidebar.winfo_reqwidth() / scale
            side_height = self._sidebar_content_height() / scale
            content_width = self.content_panel.winfo_reqwidth() / scale
            content_height = self.content_panel.winfo_reqheight() / scale
            return (
                side_width + content_width,
                max(side_height, content_height),
                side_height,
            )

        def _apply_layout_mode(
            self, width: int | None = None, height: int | None = None
        ) -> None:
            """Adapte la densité d'après les besoins réels de la fenêtre."""
            if not hasattr(self, "summary_panel"):
                return
            if width is None:
                width = self.winfo_width()
            if height is None:
                height = self.winfo_height()
            scale = self._window_scale()
            if self._normal_requirements is None:
                self._normal_requirements = self._measure_normal_requirements()
            required_width, required_height, side_height = self._normal_requirements
            logical_width = width / scale
            side_width = self.sidebar.winfo_reqwidth() / scale
            # Largeur réelle du panneau de contenu, puis largeur restante pour
            # l'accueil une fois la sidebar, les marges et la colonne de session
            # déduites. L'en-tête occupe toute la largeur du contenu.
            content_width = max(0.0, logical_width - side_width - 68.0)
            assistant_width = max(0.0, content_width - 290.0 - 20.0)
            actions = getattr(self, "heading_actions", None)
            heading_actions_width = (
                actions.winfo_reqwidth() / scale if actions is not None else 0.0
            )
            plan = plan_layout(
                width,
                height,
                scale=scale,
                required_width=required_width,
                required_height=required_height,
                sidebar_required_height=side_height,
                assistant_width=assistant_width,
                heading_width=content_width,
                heading_actions_width=heading_actions_width,
            )
            if self._layout_ready and plan.compact == self._compact_layout:
                # La largeur peut changer sans changer de mode : le retour à la
                # ligne doit suivre la largeur réellement disponible.
                if plan.compact:
                    self.welcome_title.configure(wraplength=plan.wrap_length or 0)
                    self.composer_note_label.configure(wraplength=plan.wrap_length)
                    self.heading_title.configure(
                        wraplength=plan.heading_wrap_length
                    )
                    self.heading_subtitle.configure(
                        wraplength=plan.heading_wrap_length
                    )
                self._sync_sidebar_scroll()
                return
            self._layout_ready = True
            self._compact_layout = plan.compact
            self._apply_layout_plan(plan)
            self.after_idle(self._sync_sidebar_scroll)

        def _apply_layout_plan(self, plan: LayoutPlan) -> None:
            """Applique un plan déjà décidé (séparé pour les tests)."""
            if plan.summary_visible:
                self.summary_panel.grid()
            else:
                self.summary_panel.grid_remove()
            if plan.footer_visible:
                self.footer.grid()
            else:
                self.footer.grid_remove()
            self.heading.grid_configure(pady=plan.heading_pady)
            self.content_panel.grid_configure(pady=plan.content_pady)
            if plan.welcome_icon_visible:
                self.welcome_icon.pack_forget()
                self.welcome_title.pack_forget()
                self.welcome_subtitle.pack_forget()
                self.welcome_title.configure(wraplength=0)
                self.welcome_icon.pack()
                self.welcome_title.pack(pady=(15, 5))
                self.welcome_subtitle.pack(pady=(0, 17))
            else:
                self.welcome_icon.pack_forget()
                self.welcome_subtitle.pack_forget()
                self.welcome_title.configure(
                    wraplength=plan.wrap_length or 0, justify="center"
                )
                self.welcome_title.pack_configure(pady=(0, 0))
            self.welcome_center.pack_configure(pady=plan.welcome_center_pady)
            self.prompt.configure(height=plan.prompt_height)
            if plan.budget_details_visible:
                self.budget_progress.pack_forget()
                self.budget_reset_label.pack_forget()
                self.budget_progress.pack(fill="x", pady=(10, 5))
                self.budget_reset_label.pack(fill="x")
            else:
                self.budget_progress.pack_forget()
                self.budget_reset_label.pack_forget()
            for button in self._nav_buttons:
                button.configure(height=plan.nav_height)
                button.pack_configure(pady=plan.nav_pady)
            self.side_brand.pack_configure(pady=plan.brand_pady)
            self.workspace_switch.pack_configure(pady=plan.workspace_pady)
            self.workspace_label.pack_configure(pady=plan.workspace_label_pady)
            self.config_label.pack_configure(pady=plan.config_label_pady)
            self.sidebar_bottom.pack_configure(pady=plan.sidebar_bottom_pady)
            if plan.badge_visible:
                self.layout_badge.pack(side="left", padx=(0, 8))
            else:
                self.layout_badge.pack_forget()
            self.composer_note_label.configure(
                wraplength=plan.wrap_length if plan.compact else 0
            )
            heading_wrap = plan.heading_wrap_length if plan.compact else 0
            self.heading_title.configure(wraplength=heading_wrap)
            self.heading_subtitle.configure(wraplength=heading_wrap)
            self._apply_compact_typography(plan.compact)
            # La densité change la hauteur du contenu : refaire le point après
            # que Tk a appliqué les nouveaux paddings.
            self.after(120, self._sync_sidebar_scroll)
            self.after(300, self._sync_sidebar_scroll)

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
            # Les messages de l'utilisateur et de l'agent remplacent l'accueil :
            # le transcript reste la source de vérité, la carte d'accueil ne
            # sert qu'avant la première interaction.
            if kind in {"vous", "agent", "outil", "erreur"}:
                welcome = vars(self).get("welcome_frame")
                if welcome is not None:
                    welcome.grid_remove()
                transcript = vars(self).get("transcript")
                if transcript is not None:
                    transcript.grid()

        def _set_status(self, text: str) -> None:
            self.status.configure(text=text)
            session = vars(self).get("session_status")
            if session is not None:
                session.configure(text=f"● {text}")

        def _record_task_done(self) -> None:
            """Compteur de la carte « Tâches effectuées » (session courante)."""
            count = int(vars(self).get("_tasks_done", 0)) + 1
            self._tasks_done = count
            label = vars(self).get("task_count_label")
            if label is not None:
                label.configure(text=str(count))

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

        def _install_approval_gate(self) -> None:
            """Branche les confirmations d'outils sur une boîte de dialogue."""
            from core.approval import ApprovalGate

            gate = ApprovalGate(
                decide=self._tool_approval,
                mode=str(self.settings.approvals.mode),
                enabled=bool(self.settings.approvals.enabled),
                session_grants=bool(self.settings.approvals.session_grants),
                settings=self.settings,
            )
            self.bundle.registry.set_approval(gate)

        def _tool_approval(self, request: Any) -> str:
            """Demande la décision sur le fil Tk et attend, sans contournement."""
            from ui.approval_dialog import ask_tool_approval

            outcome: dict[str, str] = {}
            done = threading.Event()

            def ask() -> None:
                try:
                    outcome["value"] = ask_tool_approval(self, request)
                except Exception:  # noqa: BLE001 - panne d'interface : refus
                    logger.exception("boîte d'approbation indisponible")
                finally:
                    done.set()

            try:
                self.after(0, ask)
            except Exception:  # noqa: BLE001 - fenêtre détruite : refus
                return "deny"
            if not done.wait(300):
                return "deny"
            return outcome.get("value", "deny")

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

        # -- Évolution autonome ---------------------------------------------

        def _open_evolution(self) -> None:
            """Panneau : version, compétences forgées, journal des cycles."""
            evolver = getattr(self.bundle, "evolver", None)
            if evolver is None:
                self._append(
                    "système",
                    "auto-évolution désactivée (evolution.enabled = false) : "
                    "aucun cycle ne tourne en arrière-plan",
                )
                self._set_status("auto-évolution désactivée")
                return

            window = ctk.CTkToplevel(self)
            window.title("Évolution autonome")
            self._fit_dialog(window, 760, 640)
            header = ctk.CTkLabel(
                window,
                text="",
                anchor="w",
                justify="left",
                wraplength=700,
                font=(
                    self.settings.ui.font_family,
                    self.settings.ui.font_size + 1,
                    "bold",
                ),
            )
            header.pack(fill="x", padx=16, pady=(14, 6))
            journal = ctk.CTkTextbox(window, wrap="word")
            journal.pack(fill="both", expand=True, padx=16, pady=6)
            journal.configure(state="disabled")

            def refresh() -> None:
                try:
                    if not window.winfo_exists():
                        return
                    header.configure(text=format_evolution_status(evolver.status()))
                    body = format_evolution_journal(evolver.journal(limit=120))
                    journal.configure(state="normal")
                    journal.delete("1.0", "end")
                    journal.insert("1.0", body)
                    journal.configure(state="disabled")
                    window.after(2000, refresh)
                except Exception:  # noqa: BLE001 - fenêtre fermée pendant le rafraîchissement
                    logger.debug("panneau d'évolution refermé", exc_info=True)

            def force() -> None:
                started = evolver.force_cycle()
                if started:
                    self._set_status("cycle d'auto-évolution lancé")
                    return
                reason = str(evolver.status().get("last_refusal") or "")
                self._set_status(
                    f"cycle refusé : {reason}"
                    if reason
                    else "cycle déjà en cours (ou auto-évolution désactivée)"
                )

            ctk.CTkButton(
                window, text="Forcer un cycle maintenant", command=force
            ).pack(pady=(0, 12))
            refresh()

        # -- Gestionnaire de clés et de modèles -----------------------------

        def _open_keys(self) -> None:
            """Pool de clés chiffrées, statuts en direct et modèle courant."""
            from core.llm_router import LLMRouter, PROVIDERS

            router = getattr(self.bundle, "router", None)
            if router is None:
                router = LLMRouter(self.settings, self.bundle.vault)
            vault = self.bundle.vault
            window = ctk.CTkToplevel(self)
            window.title("Gestionnaire de clés & LLM")
            self._fit_dialog(window, 900, 720)
            header = ctk.CTkLabel(
                window,
                text="",
                anchor="w",
                justify="left",
                wraplength=840,
                font=(self.settings.ui.font_family, self.settings.ui.font_size + 1, "bold"),
            )
            header.pack(fill="x", padx=16, pady=(14, 4))

            vault_frame = ctk.CTkFrame(window)
            vault_frame.pack(fill="x", padx=16, pady=4)
            passphrase = ctk.CTkEntry(
                vault_frame, placeholder_text="phrase secrète du coffre", show="*"
            )
            passphrase.pack(side="left", fill="x", expand=True, padx=(8, 6), pady=8)

            def unlock() -> None:
                secret = passphrase.get().strip()
                if not secret:
                    feedback.configure(text="phrase secrète requise")
                    return
                try:
                    if vault.exists:
                        vault.unlock(secret)
                    else:
                        vault.create(secret)
                except Exception as exc:  # noqa: BLE001 - message lisible à l'écran
                    feedback.configure(text=f"coffre : {exc}")
                    return
                passphrase.delete(0, "end")
                feedback.configure(text="coffre déverrouillé")

            ctk.CTkButton(vault_frame, text="Déverrouiller", width=120, command=unlock).pack(
                side="left", padx=(0, 8), pady=8
            )

            add_frame = ctk.CTkFrame(window)
            add_frame.pack(fill="x", padx=16, pady=4)
            provider_var = ctk.StringVar(value="auto")
            provider_menu = ctk.CTkOptionMenu(
                add_frame,
                values=["auto"] + [slug for slug in PROVIDERS if slug != "ollama"],
                variable=provider_var,
                width=150,
            )
            provider_menu.grid(row=0, column=0, padx=8, pady=(8, 2), sticky="n")
            keys_input = ctk.CTkTextbox(add_frame, height=80, wrap="none")
            keys_input.grid(row=0, column=1, padx=8, pady=(8, 2), sticky="ew")
            add_frame.grid_columnconfigure(1, weight=1)
            feedback = ctk.CTkLabel(
                window,
                text="",
                anchor="w",
                justify="left",
                wraplength=840,
            )
            feedback.pack(fill="x", padx=16)

            def add_keys() -> None:
                lines = [
                    line.strip()
                    for line in keys_input.get("1.0", "end").splitlines()
                    if line.strip()
                ]
                if not lines:
                    feedback.configure(text="aucune clé à ajouter")
                    return
                added = 0
                errors: list[str] = []
                for index, key in enumerate(lines, start=1):
                    try:
                        router.add_key(key, provider=provider_var.get())
                        added += 1
                    except Exception as exc:  # noqa: BLE001 - une clé fautive n'arrête pas le lot
                        errors.append(f"ligne {index} : {exc}")
                if added:
                    keys_input.delete("1.0", "end")
                feedback.configure(
                    text=f"{added} clé(s) ajoutée(s)"
                    + (f" · refus : {' ; '.join(errors[:3])}" if errors else "")
                )

            ctk.CTkButton(
                add_frame, text="Ajouter le lot", width=140, command=add_keys
            ).grid(row=0, column=2, padx=8, pady=(8, 2), sticky="n")

            keys_frame = ctk.CTkScrollableFrame(window, label_text="Clés enregistrées")
            keys_frame.pack(fill="both", expand=True, padx=16, pady=8)
            rows_frame = ctk.CTkFrame(keys_frame)
            rows_frame.pack(fill="x", expand=True)

            def reset(key_id: str) -> None:
                if router.reset_key(key_id):
                    feedback.configure(text="clé réactivée")
                else:
                    feedback.configure(text="clé introuvable")
                refresh()

            def remove(key_id: str) -> None:
                try:
                    removed = router.remove_key(key_id)
                except Exception as exc:  # noqa: BLE001 - message lisible à l'écran
                    feedback.configure(text=f"suppression : {exc}")
                    return
                feedback.configure(
                    text="clé supprimée" if removed else "clé introuvable"
                )
                refresh()

            table = KeyTable(
                rows_frame, ctk, on_reset=reset, on_remove=remove
            )

            def refresh() -> None:
                try:
                    if not window.winfo_exists():
                        return
                    vault.check_timeout()
                    state = "déverrouillé" if vault.unlocked else "verrouillé"
                    mode = "actif" if self.settings.llm_router.enabled else "désactivé"
                    current = router.current_model or "aucun appel réussi"
                    header.configure(
                        text=f"Modèle courant : {current} · routeur {mode} · coffre {state}"
                    )
                    table.render(router.key_statuses())
                    window.after(2000, refresh)
                except Exception:  # noqa: BLE001 - fenêtre refermée pendant le rafraîchissement
                    logger.debug("panneau des clés refermé", exc_info=True)

            refresh()

        # -- Directive Première ---------------------------------------------

        def _open_prime_directive(self) -> None:
            """Tableau de bord des opportunités et du temps libéré."""
            prime = getattr(self.bundle, "prime", None)
            if prime is None:
                self._append("système", "Directive Première indisponible")
                self._set_status("Directive Première indisponible")
                return

            window = ctk.CTkToplevel(self)
            window.title("Directive Première")
            self._fit_dialog(window, 760, 620)
            header = ctk.CTkLabel(
                window,
                text="",
                anchor="w",
                justify="left",
                wraplength=700,
                font=(
                    self.settings.ui.font_family,
                    self.settings.ui.font_size + 1,
                    "bold",
                ),
            )
            header.pack(fill="x", padx=16, pady=(14, 6))
            body = ctk.CTkTextbox(window, wrap="word")
            body.pack(fill="both", expand=True, padx=16, pady=6)
            body.configure(state="disabled")

            def refresh() -> None:
                try:
                    if not window.winfo_exists():
                        return
                    data = prime.dashboard()
                    header.configure(
                        text=(
                            "Directive Première · "
                            f"{len(data.get('opportunities') or [])} opportunité(s) · "
                            f"{float(data.get('time_saved_hours') or 0):.2f} h libérées"
                        )
                    )
                    body.configure(state="normal")
                    body.delete("1.0", "end")
                    body.insert("1.0", prime.format_dashboard())
                    body.configure(state="disabled")
                    window.after(3000, refresh)
                except Exception:  # noqa: BLE001 - fenêtre fermée pendant le rafraîchissement
                    logger.debug("panneau Directive Première refermé", exc_info=True)

            refresh()

        # -- Planificateur de tâches Windows --------------------------------

        def _open_scheduler(self) -> None:
            """Fenêtre dédiée : inscrire ou désinscrire la tâche Windows."""
            window = ctk.CTkToplevel(self)
            window.title("Planificateur de tâches Windows")
            dialog_width, _ = self._fit_dialog(window, 560, 300)
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
                wraplength=max(280, dialog_width - 32),
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
            dialog_width, _ = self._fit_dialog(window, 780, 520)
            window.transient(self)
            frame = ctk.CTkScrollableFrame(window, label_text=str(path))
            frame.pack(fill="both", expand=True, padx=10, pady=10)
            ctk.CTkLabel(
                frame,
                text=format_autonomy_journal(records),
                justify="left",
                anchor="w",
                wraplength=max(280, dialog_width - 70),
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
            dialog_width, _ = self._fit_dialog(window, 780, 460)
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
                    row,
                    text=detail,
                    anchor="w",
                    justify="left",
                    wraplength=max(260, dialog_width - 280),
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
            dialog_width, _ = self._fit_dialog(window, 760, 480)
            window.transient(self)
            toolbar = ctk.CTkFrame(window)
            toolbar.pack(fill="x", padx=10, pady=(10, 0))
            report_label = ctk.CTkLabel(
                toolbar,
                text="",
                anchor="w",
                justify="left",
                wraplength=max(280, dialog_width - 40),
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
                    self._record_task_done()
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

            self._refresh_budget()
            interval = max(30, int(self.settings.ui.stream_refresh_ms))
            self._pump_id = self.after(interval, self._pump)

        def _refresh_budget(self) -> None:
            """Met à jour la jauge au plus toutes les trente secondes."""
            now = time.monotonic()
            # `vars` et non `getattr` : les tests construisent la fenêtre avec
            # `object.__new__`, dont le `__getattr__` Tk récurserait.
            local = vars(self)
            if now - float(local.get("_budget_at", 0.0)) < 30.0:
                return
            self._budget_at = now
            try:
                from core.budget import check

                state = check(self.settings)
            except Exception:  # noqa: BLE001 - une jauge ne casse pas l'interface
                logger.debug("budget illisible", exc_info=True)
                return
            text = f"budget : {state.usage.day_cost:.2f} USD aujourd'hui"
            remaining = state.remaining
            if remaining != float("inf"):
                text += f" · reste {remaining:.2f} USD"
            if not state.allowed:
                text += " · plafond atteint"
            label = local.get("budget_label")
            card = local.get("budget_card_label")
            if card is not None:
                card.configure(text=f"{state.usage.day_cost:.2f} $")
            limit = float(getattr(state, "limits", {}).get("day") or 0.0)
            if label is not None:
                if limit > 0:
                    label.configure(
                        text=f"{state.usage.day_cost:.2f} / {limit:.2f} $"
                    )
                else:
                    label.configure(text=f"{state.usage.day_cost:.2f} $ · sans plafond")
            progress = local.get("budget_progress")
            if progress is not None:
                progress.set(
                    min(1.0, state.usage.day_cost / limit) if limit > 0 else 0.0
                )

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
                evolver = getattr(self.bundle, "evolver", None)
                if evolver is not None:
                    evolver.stop()
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
